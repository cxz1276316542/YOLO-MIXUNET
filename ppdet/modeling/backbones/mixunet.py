# copyright (c) 2022 PaddlePaddle Authors. All Rights Reserve.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import numpy as np
import paddle
import paddle.nn as nn
import paddle.nn.functional as F

from .transformer_utils import (
    trunc_normal_, zeros_, ones_, to_2tuple, DropPath, Identity)
from .swin_transformer import Mlp, window_partition, window_reverse
import math
from ppdet.modeling.shape_spec import ShapeSpec
from ppdet.core.workspace import register, serializable
# TODO: update the urls of the pre-trained models

__all__ = ['MixUnet']



def window_partition2(x, window_size):
    """ Split the feature map to windows.
    B, C, H, W --> B * H // win * W // win x win*win x C

    Args:
        x: (B, C, H, W)
        window_size (tuple[int]): window size

    Returns:
        windows: (num_windows*B, window_size * window_size, C)
    """
    B, C, H, W = x.shape
    x = x.reshape([B, C, H // window_size[0], window_size[0],
                   W // window_size[1], window_size[1]])
    windows = x.transpose([0, 2, 4, 3, 5, 1]).reshape(
        [-1, window_size[0] * window_size[1], C])
    return windows


def window_reverse2(windows, window_size, H, W, C):
    """ Windows reverse to feature map.
    B * H // win * W // win x win*win x C --> B, C, H, W

    Args:
        windows: (num_windows*B, window_size, window_size, C)
        window_size (tuple[int]): Window size
        H (int): Height of image
        W (int): Width of image

    Returns:
        x: (B, C, H, W)
    """
    x = windows.reshape([-1, H // window_size[0], W // window_size[1],
                         window_size[0], window_size[1], C])
    x = x.transpose([0, 5, 1, 3, 2, 4]).reshape([-1, C, H, W])
    return x


class MixingAttention(nn.Layer):
    r""" Mixing Attention Module.
    Modified from Window based multi-head self attention (W-MSA) module
    with relative position bias.

    Args:
        dim (int): Number of input channels.
        window_size (tuple[int]): The height and width of the window.
        dwconv_kernel_size (int): The kernel size for dw-conv
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to
            query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale
            of head_dim ** -0.5 if set
        attn_drop (float, optional): Dropout ratio of attention weight.
            Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
    """
    def __init__(self,
                 dim,
                 window_size,
                 dwconv_kernel_size,
                 num_heads,
                 qkv_bias=True,
                 qk_scale=None,
                 attn_drop=0.,
                 proj_drop=0.):
        super().__init__()
        self.dim = dim
        attn_dim = dim // 2
        self.window_size = window_size  # Wh, Ww
        self.dwconv_kernel_size = dwconv_kernel_size
        self.num_heads = num_heads
        head_dim = attn_dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        # define a parameter table of relative position bias
        self.relative_position_bias_table = self.create_parameter(
            shape=((2 * window_size[0] - 1) * (2 * window_size[1] - 1),
                   num_heads),
            default_initializer=zeros_)
        self.add_parameter("relative_position_bias_table",
                           self.relative_position_bias_table)

        # get pair-wise relative position index for each token
        # inside the window
        relative_coords = self._get_rel_pos()
        self.relative_position_index = relative_coords.sum(-1)  # Wh*Ww, Wh*Ww
        self.register_buffer("relative_position_index",
                             self.relative_position_index)
        # prev proj layer
        self.proj_attn = nn.Linear(dim, dim // 2)
        self.proj_attn_norm = nn.LayerNorm(dim // 2)
        self.proj_cnn = nn.Linear(dim, dim)
        self.proj_cnn_norm = nn.LayerNorm(dim)

        # conv branch
        self.dwconv3x3 = nn.Sequential(
            nn.Conv2D(
                dim, dim,
                kernel_size=self.dwconv_kernel_size,
                padding=self.dwconv_kernel_size // 2,
                groups=dim
            ),
            nn.BatchNorm(dim),
            nn.GELU()
        )
        self.channel_interaction = nn.Sequential(
            nn.Conv2D(dim, dim // 8, kernel_size=1),
            nn.BatchNorm(dim // 8),
            nn.GELU(),
            nn.Conv2D(dim // 8, dim // 2, kernel_size=1),
        )
        self.projection = nn.Conv2D(dim, dim // 2, kernel_size=1)
        self.conv_norm = nn.BatchNorm(dim // 2)

        # window-attention branch
        self.qkv = nn.Linear(dim // 2, dim // 2 * 3, bias_attr=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.spatial_interaction = nn.Sequential(
            nn.Conv2D(dim // 2, dim // 16, kernel_size=1),
            nn.BatchNorm(dim // 16),
            nn.GELU(),
            nn.Conv2D(dim // 16, 1, kernel_size=1)
        )
        self.attn_norm = nn.LayerNorm(dim // 2)

        # final projection
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        trunc_normal_(self.relative_position_bias_table)
        self.softmax = nn.Softmax(axis=-1)

    def _get_rel_pos(self):
        """ Get pair-wise relative position index for each token inside the window.

        Args:
            window_size (tuple[int]): window size
        """
        coords_h = paddle.arange(self.window_size[0])
        coords_w = paddle.arange(self.window_size[1])
        # 2, Wh, Ww
        coords = paddle.stack(paddle.meshgrid([coords_h, coords_w]))
        coords_flatten = paddle.flatten(coords, 1)  # 2, Wh*Ww
        coords_flatten_1 = coords_flatten.unsqueeze(axis=2)
        coords_flatten_2 = coords_flatten.unsqueeze(axis=1)
        relative_coords = coords_flatten_1 - coords_flatten_2
        relative_coords = relative_coords.transpose(
            [1, 2, 0])  # Wh*Ww, Wh*Ww, 2
        relative_coords[:, :, 0] += self.window_size[
            0] - 1  # shift to start from 0
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        return relative_coords

    def forward(self, x, H, W, mask=None):
        """
        Args:
            x: input features with shape of (num_windows*B, N, C)
            H: the height of the feature map
            W: the width of the feature map
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww)
                or None
        """
        # B * H // win * W // win x win*win x C
        x_atten = self.proj_attn_norm(self.proj_attn(x))
        x_cnn = self.proj_cnn_norm(self.proj_cnn(x))
        # B * H // win * W // win x win*win x C --> B, C, H, W
        x_cnn = window_reverse2(x_cnn, self.window_size, H, W, x_cnn.shape[-1])

        # conv branch
        x_cnn = self.dwconv3x3(x_cnn)
        channel_interaction = self.channel_interaction(
            F.adaptive_avg_pool2d(x_cnn, output_size=1))
        x_cnn = self.projection(x_cnn)

        # attention branch
        B_, N, C = x_atten.shape
        qkv = self.qkv(x_atten).reshape(
            [B_, N, 3, self.num_heads, C // self.num_heads]).transpose(
                [2, 0, 3, 1, 4])
        # make torchscript happy (cannot use tensor as tuple)
        q, k, v = qkv[0], qkv[1], qkv[2]
        # channel interaction
        x_cnn2v = F.sigmoid(channel_interaction).reshape(
            [-1, 1, self.num_heads, 1, C // self.num_heads])
        v = v.reshape(
            [x_cnn2v.shape[0], -1, self.num_heads, N, C // self.num_heads])
        v = v * x_cnn2v
        v = v.reshape([-1, self.num_heads, N, C // self.num_heads])

        q = q * self.scale
        attn = paddle.mm(q, k.transpose([0, 1, 3, 2]))

        index = self.relative_position_index.reshape([-1])

        relative_position_bias = paddle.index_select(
            self.relative_position_bias_table, index)
        relative_position_bias = relative_position_bias.reshape([
            self.window_size[0] * self.window_size[1],
            self.window_size[0] * self.window_size[1], -1
        ])  # Wh*Ww,Wh*Ww,nH
        relative_position_bias = relative_position_bias.transpose(
            [2, 0, 1])  # nH, Wh*Ww, Wh*Ww
        attn = attn + relative_position_bias.unsqueeze(0)

        if mask is not None:
            nW = mask.shape[0]
            attn = attn.reshape([B_ // nW, nW, self.num_heads, N, N]) + \
                mask.unsqueeze(1).unsqueeze(0)
            attn = attn.reshape([-1, self.num_heads, N, N])
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        x_atten = paddle.mm(attn, v).transpose([0, 2, 1, 3]).reshape(
            [B_, N, C])

        # spatial interaction
        x_spatial = window_reverse2(x_atten, self.window_size, H, W, C)
        spatial_interaction = self.spatial_interaction(x_spatial)
        x_cnn = F.sigmoid(spatial_interaction) * x_cnn
        x_cnn = self.conv_norm(x_cnn)
        # B, C, H, W --> B * H // win * W // win x win*win x C
        x_cnn = window_partition2(x_cnn, self.window_size)

        # concat
        x_atten = self.attn_norm(x_atten)
        x = paddle.concat([x_atten, x_cnn], axis=-1)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

class Bottleneck(nn.Layer):
    # 这里对应是4,对应每层中的64，64，256
    expansion = 2

    def __init__(self, in_channel, out_channel, stride=1):
        super(Bottleneck, self).__init__()

        self.conv1 = nn.Conv2D(in_channels=in_channel, out_channels=out_channel,
                               kernel_size=3, stride=1,  padding=1, groups=in_channel, bias_attr=False)
        self.bn1 = nn.BatchNorm2D(out_channel)

        self.conv2 = nn.Conv2D(in_channels=out_channel, out_channels=out_channel//self.expansion,
                               kernel_size=1, stride=1,  bias_attr=False)
        self.bn2 = nn.BatchNorm2D(out_channel//self.expansion)

        self.conv3 = nn.Conv2D(in_channels=out_channel//self.expansion, out_channels=out_channel,
                               kernel_size=1, stride=1, bias_attr=False)
        self.bn3 = nn.BatchNorm2D(out_channel )
        self.conv4 = nn.Conv2D(in_channels=in_channel, out_channels=out_channel,
                               kernel_size=3, stride=1, padding=1, groups=in_channel, bias_attr=False)
        self.bn4 = nn.BatchNorm2D(out_channel)
        self.relu = nn.ReLU()


    def forward(self, x):
        identity = x

        out = self.conv1(x)

        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        #out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        #out = self.bn3(out)
        out = self.conv4(out)
        out = self.bn4(out)
        out += identity
        out = self.relu(out)

        return out
class MixingBlock(nn.Layer):
    r""" Mixing Block in MixFormer.
    Modified from Swin Transformer Block.

    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads.
        window_size (int): Window size.
        dwconv_kernel_size (int): kernel size for depth-wise convolution.
        shift_size (int): Shift size for SW-MSA.
            We do not use shift in MixFormer. Default: 0
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        qkv_bias (bool, optional): If True, add a learnable bias to
            query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of
            head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float, optional): Stochastic depth rate. Default: 0.0
        act_layer (nn.Layer, optional): Activation layer. Default: nn.GELU
        norm_layer (nn.Layer, optional): Normalization layer.
            Default: nn.LayerNorm
    """

    def __init__(self,
                 dim,
                 num_heads,
                 window_size=7,
                 dwconv_kernel_size=3,
                 shift_size=0,
                 mlp_ratio=4.,
                 qkv_bias=True,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_path=0.,
                 act_layer=nn.GELU,
                 norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.shift_size = shift_size
        self.mlp_ratio = mlp_ratio
        assert self.shift_size == 0, "No shift in MixUnet"

        self.norm1 = norm_layer(dim)
        self.attn = MixingAttention(
            dim,
            window_size=to_2tuple(self.window_size),
            dwconv_kernel_size=dwconv_kernel_size,
            num_heads=num_heads,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=drop)

        self.drop_path = DropPath(drop_path) if drop_path > 0. else Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim,
                       hidden_features=mlp_hidden_dim,
                       act_layer=act_layer,
                       drop=drop)
        self.bottle_neck_cnn = Bottleneck(in_channel=dim, out_channel=dim, stride=1)
        self.H = None
        self.W = None

    def forward(self, x, mask_matrix):
        """ Forward function.

        Args:
            x: Input feature, tensor size (B, H*W, C).
            H, W: Spatial resolution of the input feature.
            mask_matrix: Attention mask for cyclic shift.
        """
        B, L, C = x.shape
        H, W = self.H, self.W
        assert L == H * W, "input feature has wrong size"

        shortcut = x
        x = self.norm1(x)
        x = x.reshape([B, H, W, C])
        x = x.transpose([0, 3, 1, 2])
        x = self.bottle_neck_cnn(x)
        x = x.transpose([0, 2, 3, 1])
        # pad feature maps to multiples of window size
        pad_l = pad_t = 0
        pad_r = (self.window_size - W % self.window_size) % self.window_size
        pad_b = (self.window_size - H % self.window_size) % self.window_size
        x = F.pad(x, [0, pad_l, 0, pad_b, 0, pad_r, 0, pad_t])
        _, Hp, Wp, _ = x.shape

        # cyclic shift
        if self.shift_size > 0:
            shifted_x = paddle.roll(
                x, shifts=(-self.shift_size, -self.shift_size), axis=(1, 2))
            attn_mask = mask_matrix
        else:
            shifted_x = x
            attn_mask = None

        # partition windows
        x_windows = window_partition(
            shifted_x, self.window_size)  # nW*B, window_size, window_size, C
        x_windows = x_windows.reshape(
            [-1, self.window_size * self.window_size,
             C])  # nW*B, window_size*window_size, C

        # W-MSA/SW-MSA
        # nW*B, window_size*window_size, C
        attn_windows = self.attn(
            x_windows, Hp, Wp, mask=attn_mask)

        # merge windows
        attn_windows = attn_windows.reshape(
            [-1, self.window_size, self.window_size, C])
        shifted_x = window_reverse(attn_windows, self.window_size, Hp,
                                   Wp)  # B H' W' C

        # reverse cyclic shift
        if self.shift_size > 0:
            x = paddle.roll(
                shifted_x,
                shifts=(self.shift_size, self.shift_size),
                axis=(1, 2))
        else:
            x = shifted_x

        if pad_r > 0 or pad_b > 0:
            x = x[:, :H, :W, :]

        x = x.reshape([B, H * W, C])

        # FFN
        x = shortcut + self.drop_path(x)
        x = x + self.drop_path(self.mlp(self.norm2(x)))

        return x
## 构建卷积块
class BaseConv(nn.Layer):
    def __init__(self, in_channels, out_channels, kernel_size, stride, groups=1, act='silu'):
        super().__init__()
        padding = (kernel_size-1)//2
        self.conv = nn.Conv2D(in_channels, out_channels, kernel_size, stride, padding, groups=groups)
        # self.bn = nn.BatchNorm2D(out_channels,momentum=0.03, epsilon=0.001)
        if act == 'silu':
            self.act = nn.Silu()
        elif act == 'relu':
            self.act = nn.ReLU()
        elif act == 'lrelu':
            self.act = nn.LeakyReLU(0.1)
    def forward(self, x):
        return self.act(self.conv(x))

## Focus层
class Focus(nn.Layer):
    def __init__(self, in_channels, out_channels, ksize=1, stride=1, act="silu"):
        super().__init__()
        self.conv = BaseConv(in_channels * 4, out_channels, ksize, stride, act=act)

    def forward(self, x):
        # 分别获得4个2倍下采样结果
        patch_1 = x[...,  ::2,  ::2]
        patch_2 = x[..., 1::2,  ::2]
        patch_3 = x[...,  ::2, 1::2]
        patch_4 = x[..., 1::2, 1::2]
        # 沿通道方向拼接4个下采样结果
        x = paddle.concat((patch_1, patch_2, patch_3, patch_4), axis=1)
        # 拼接结果做卷积
        out = self.conv(x)
        return out
class ConvMerging(nn.Layer):
    r""" Conv Merging Layer.

    Args:
        dim (int): Number of input channels.
        out_dim (int): Output channels after the merging layer.
        norm_layer (nn.Module, optional): Normalization layer.
            Default: nn.LayerNorm
    """

    def __init__(self, dim, out_dim, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.out_dim = out_dim
        self.reduction = Focus(dim,out_dim)
        self.norm = nn.BatchNorm(dim)

    def forward(self, x, H, W):
        """
        Args:
            x: Input feature, tensor size (B, H*W, C).
            H, W: Spatial resolution of the input feature.
        """
        B, L, C = x.shape
        assert L == H * W, "input feature has wrong size"
        assert H % 2 == 0 and W % 2 == 0, f"x size ({H}*{W}) are not even."

        x = x.reshape([B, H, W, C]).transpose([0, 3, 1, 2])

        x = self.norm(x)
        # B, C, H, W -> B, H*W, C
        x = self.reduction(x).flatten(2).transpose([0, 2, 1])
        return x


class BasicLayer(nn.Layer):
    """ A basic layer for one stage in MixFormer.
    Modified from Swin Transformer BasicLayer.

    Args:
        dim (int): Number of input channels.
        depth (int): Number of blocks.
        num_heads (int): Number of attention heads.
        window_size (int): Local window size.
        dwconv_kernel_size (int): kernel size for depth-wise convolution.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        qkv_bias (bool, optional): If True, add a learnable bias to
            query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of
            head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float | tuple[float], optional): Stochastic depth rate.
            Default: 0.0
        norm_layer (nn.Layer, optional): Normalization layer.
            Default: nn.LayerNorm
        downsample (nn.Layer | None, optional): Downsample layer at the end
            of the layer. Default: None
        out_dim (int): Output channels for the downsample layer. Default: 0.
    """

    def __init__(self,
                 dim,
                 depth,
                 num_heads,
                 window_size=7,
                 dwconv_kernel_size=3,
                 mlp_ratio=4.,
                 qkv_bias=True,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_path=0.,
                 norm_layer=nn.LayerNorm,
                 downsample=None,
                 out_dim=0):
        super().__init__()
        self.window_size = window_size
        self.depth = depth

        # build blocks
        self.blocks = nn.LayerList([
            MixingBlock(
                dim=dim,
                num_heads=num_heads,
                window_size=window_size,
                dwconv_kernel_size=dwconv_kernel_size,
                shift_size=0,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop,
                attn_drop=attn_drop,
                drop_path=drop_path[i]
                if isinstance(drop_path, (np.ndarray, list)) else drop_path,
                norm_layer=norm_layer) for i in range(depth)
        ])

        # patch merging layer
        if downsample is not None:
            self.downsample = downsample(
                dim=dim, out_dim=out_dim, norm_layer=norm_layer)
        else:
            self.downsample = None

    def forward(self, x, H, W):
        """ Forward function.

        Args:
            x: Input feature, tensor size (B, H*W, C).
            H, W: Spatial resolution of the input feature.
        """
        for blk in self.blocks:
            blk.H, blk.W = H, W
            x = blk(x, None)
        if self.downsample is not None:
            x_down = self.downsample(x, H, W)
            Wh, Ww = (H + 1) // 2, (W + 1) // 2
            return H, W, x_down, Wh, Ww
        else:
            return H, W, x, H, W

class BasicLayer_up(nn.Layer):
    """ A basic Swin Transformer layer for one stage.

    Args:
        dim (int): Number of input channels.
        input_resolution (tuple[int]): Input resolution.
        depth (int): Number of blocks.
        num_heads (int): Number of attention heads.
        window_size (int): Local window size.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
        norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
        downsample (nn.Module | None, optional): Downsample layer at the end of the layer. Default: None
        use_checkpoint (bool): Whether to use checkpointing to save memory. Default: False.
    """

    def __init__(self, dim, input_resolution, depth, num_heads, window_size, dwconv_kernel_size=3,
                 mlp_ratio=4., qkv_bias=True, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., norm_layer=nn.LayerNorm, upsample=None):

        super().__init__()
        self.dim = dim
        self.input_resolution = input_resolution
        self.depth = depth


        # build blocks
        self.blocks = nn.LayerList([
            MixingBlock(dim=dim,
                                 num_heads=num_heads, window_size=window_size, dwconv_kernel_size=dwconv_kernel_size,
                                 shift_size=0 ,
                                 mlp_ratio=mlp_ratio,
                                 qkv_bias=qkv_bias, qk_scale=qk_scale,
                                 drop=drop, attn_drop=attn_drop,
                                 drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path,
                                 norm_layer=norm_layer)
            for i in range(depth)])

        # patch merging layer
        if upsample is not None:
            self.upsample = PatchExpand(input_resolution, dim=dim, dim_scale=2, norm_layer=norm_layer)
        else:
            self.upsample = None

    def forward(self, x, H, W):

        for blk in self.blocks:
                blk.H, blk.W = H, W
                # blk(): -> [B, H*W, C]
                x = blk(x, None)
        output = x
        if self.upsample is not None:
            x = self.upsample(x)
        return x,output

class ConvEmbed(nn.Layer):
    r""" Image to Conv Stem Embedding

    Args:
        img_size (int): Image size.  Default: 224.
        patch_size (int): Patch token size. Default: 4.
        in_chans (int): Number of input image channels. Default: 3.
        embed_dim (int): Number of linear projection output channels.
            Default: 96.
        norm_layer (nn.Module, optional): Normalization layer.
            Default: None
    """

    def __init__(self,
                 img_size=224,
                 patch_size=4,
                 in_chans=3,
                 embed_dim=96,
                 norm_layer=None):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        patches_resolution = [
            img_size[0] // patch_size[0], img_size[1] // patch_size[1]]
        self.img_size = img_size
        self.patch_size = patch_size
        self.patches_resolution = patches_resolution
        self.num_patches = patches_resolution[0] * patches_resolution[1]

        self.in_chans = in_chans
        self.embed_dim = embed_dim

        self.stem = nn.Sequential(
            nn.Conv2D(in_chans, embed_dim // 2, kernel_size=3,
                      stride=patch_size[0] // 2, padding=1),
            nn.BatchNorm(embed_dim // 2),
            nn.GELU(),
            nn.Conv2D(embed_dim // 2, embed_dim // 2, kernel_size=3,
                      stride=1, padding=1),
            nn.BatchNorm(embed_dim // 2),
            nn.GELU(),
            nn.Conv2D(embed_dim // 2, embed_dim // 2, kernel_size=3,
                      stride=1, padding=1),
            nn.BatchNorm(embed_dim // 2),
            nn.GELU(),
        )
        self.proj = nn.Conv2D(embed_dim // 2, embed_dim,
                              kernel_size=patch_size[0] // 2,
                              stride=patch_size[0] // 2)
        if norm_layer is not None:
            self.norm = norm_layer(embed_dim)
        else:
            self.norm = None

    def forward(self, x):

        B, C, H, W = x.shape
        if W % self.patch_size[1] != 0:
            x = F.pad(
                x, [0, self.patch_size[1] - W % self.patch_size[1], 0, 0])
        if H % self.patch_size[0] != 0:
            x = F.pad(
                x, [0, 0, 0, self.patch_size[0] - H % self.patch_size[0]])

        x = self.stem(x)
        x = self.proj(x)
        if self.norm is not None:
            _, _, Wh, Ww = x.shape
        x = x.flatten(2).transpose([0, 2, 1])  # B Ph*Pw C
        if self.norm is not None:
            x = self.norm(x)
        x = x.transpose([0, 2, 1])
        x = x.reshape([-1, self.embed_dim, Wh, Ww])
        return x

class PatchExpand(nn.Layer):
    def __init__(self, input_resolution, dim, dim_scale=2, norm_layer=nn.LayerNorm):
        super().__init__()
        self.input_resolution = input_resolution
        self.dim = dim
        self.expand = nn.Linear(dim, 2*dim, bias_attr=False) if dim_scale==2 else nn.Identity()
        self.norm = norm_layer(dim // dim_scale)

    def forward(self, x):
        """
        x: B, H*W, C
        """
        H, W = self.input_resolution
        x = self.expand(x)
        B, L, C = x.shape

        assert L == H * W, "input feature has wrong size"

        x = x.reshape([B, H, W, C])
        #x = rearrange(x, 'b h w (p1 p2 c)-> b (h p1) (w p2) c', p1=2, p2=2, c=C//4)
        x = x.reshape([B, 2*H, 2*W, C//4])
        x = x.reshape([B,-1,C//4])
        x= self.norm(x)

        return x
@register
@serializable
class MixUnet(nn.Layer):
    """ A PaddlePaddle impl of MixFormer:
    MixFormer: Mixing Features across Windows and Dimensions (CVPR 2022, Oral)

    Modified from Swin Transformer.

    Args:
        img_size (int | tuple(int)): Input image size. Default 224
        patch_size (int | tuple(int)): Patch size. Default: 4
        in_chans (int): Number of input image channels. Default: 3
        num_classes (int): Number of classes for classification head.
            Default: 1000
        embed_dim (int): Patch embedding dimension. Default: 96
        depths (tuple(int)): Depth of each Swin Transformer layer.
        num_heads (tuple(int)): Number of attention heads in different layers.
        window_size (int): Window size. Default: 7
        dwconv_kernel_size (int): kernel size for depth-wise convolution.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim. Default: 4
        qkv_bias (bool): If True, add a learnable bias to query, key, value.
            Default: True
        qk_scale (float): Override default qk scale of head_dim ** -0.5 if set.
            Default: None
        drop_rate (float): Dropout rate. Default: 0
        attn_drop_rate (float): Attention dropout rate. Default: 0
        drop_path_rate (float): Stochastic depth rate. Default: 0.1
        norm_layer (nn.Layer): Normalization layer. Default: nn.LayerNorm.
        ape (bool): If True, add absolute position embedding to the
            patch embedding. Default: False
        patch_norm (bool): If True, add normalization after patch embedding.
            Default: True
        use_checkpoint (bool): Whether to use checkpointing to save memory.
            Default: False
    """

    def __init__(self,
                 img_size=640,
                 patch_size=4,
                 in_chans=3,
                 class_num=1000,
                 embed_dim=24,
                 depths=[2, 2, 6, 2],
                 num_heads=[3, 6, 12, 24],
                 window_size=7,
                 dwconv_kernel_size=3,
                 mlp_ratio=4.,
                 qkv_bias=True,
                 qk_scale=None,
                 drop_rate=0.,
                 attn_drop_rate=0.,
                 drop_path_rate=0.1,
                 norm_layer=nn.LayerNorm,
                 out_indices=(1, 2, 3),
                 ape=False,
                 patch_norm=True,
                 frozen_stages=-1,
                 pretrained=None,
                 **kwargs):
        super(MixUnet, self).__init__()
        self.num_classes = num_classes = class_num
        self.num_layers = len(depths)
        if isinstance(embed_dim, int):
            embed_dim = [embed_dim * 2 ** i_layer
                         for i_layer in range(self.num_layers)]
        assert isinstance(embed_dim, list) and \
            len(embed_dim) == self.num_layers
        self.embed_dim = embed_dim
        self.ape = ape
        self.patch_norm = patch_norm
        self.frozen_stages = frozen_stages

        #num_features = [int(embed_dim * 2 ** i) for i in range(self.num_layers)]
        self.num_features = embed_dim
        self.num_feature = int(self.embed_dim[-1])

        self.num_channel = embed_dim

        self.mlp_ratio = mlp_ratio
        self.out_indices = out_indices
        # split image into patches
        self.patch_embed = ConvEmbed(
            img_size=img_size,
            patch_size=patch_size,
            in_chans=in_chans,
            embed_dim=embed_dim[0],
            norm_layer=norm_layer if self.patch_norm else None)
        num_patches = self.patch_embed.num_patches
        patches_resolution = self.patch_embed.patches_resolution
        self.patches_resolution = patches_resolution

        # absolute position embedding
        if self.ape:
            self.absolute_pos_embed = self.create_parameter(
                shape=(1, num_patches, self.embed_dim[0]),
                default_initializer=zeros_)
            self.add_parameter(
                "absolute_pos_embed", self.absolute_pos_embed)
            trunc_normal_(self.absolute_pos_embed)

        self.pos_drop = nn.Dropout(p=drop_rate)

        # stochastic depth
        # stochastic depth decay rule
        dpr = np.linspace(0, drop_path_rate,
                          sum(depths)).tolist()

        # build layers
        self.layers = nn.LayerList()
        for i_layer in range(self.num_layers):
            layer = BasicLayer(
                dim=int(self.embed_dim[i_layer]),
                depth=depths[i_layer],
                num_heads=num_heads[i_layer],
                window_size=window_size,
                dwconv_kernel_size=dwconv_kernel_size,
                mlp_ratio=self.mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop_rate,
                attn_drop=attn_drop_rate,
                drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                norm_layer=norm_layer,
                downsample=ConvMerging
                if (i_layer < self.num_layers - 1) else None,
                out_dim=int(self.embed_dim[i_layer + 1])
                if (i_layer < self.num_layers - 1) else 0)
            self.layers.append(layer)

        ############################
        # build decoder layers
        self.layers_up = nn.LayerList()
        self.concat_back_dim = nn.LayerList()
        for i_layer in range(self.num_layers-1):
            concat_linear = nn.Linear(2 * int(embed_dim[0] * 2 ** (self.num_layers - 1 - i_layer)),
                                          int(embed_dim[0] * 2 ** (
                                                  self.num_layers - 1 - i_layer))) if i_layer > 0 else nn.Identity()
            if i_layer == 0:
                layer_up = PatchExpand(
                        input_resolution=(patches_resolution[0] // (2 ** (self.num_layers - 1 - i_layer)),
                                          patches_resolution[1] // (2 ** (self.num_layers - 1 - i_layer))),
                        dim=int(embed_dim[0] * 2 ** (self.num_layers - 1 - i_layer)), dim_scale=2,
                        norm_layer=norm_layer)
            else:
                layer_up = BasicLayer_up(dim=int(embed_dim[0] * 2 ** (self.num_layers - 1 - i_layer)),
                                             input_resolution=(
                                                 patches_resolution[0] // (2 ** (self.num_layers - 1 - i_layer)),
                                                 patches_resolution[1] // (2 ** (self.num_layers - 1 - i_layer))),
                                             depth=depths[(self.num_layers - 1 - i_layer)],
                                             num_heads=num_heads[(self.num_layers - 1 - i_layer)],
                                             window_size=window_size,
                                             mlp_ratio=self.mlp_ratio,
                                             qkv_bias=qkv_bias, qk_scale=qk_scale,
                                             drop=drop_rate, attn_drop=attn_drop_rate,
                                             drop_path=dpr[sum(depths[:(self.num_layers - 1 - i_layer)]):sum(
                                                 depths[:(self.num_layers - 1 - i_layer) + 1])],
                                             norm_layer=norm_layer,
                                             upsample=PatchExpand if (i_layer < self.num_layers ) else None,
                                             )
            self.layers_up.append(layer_up)
            self.concat_back_dim.append(concat_linear)
        ###################


        self.norm = norm_layer(self.num_feature)
        ##########
        self.norm_up = norm_layer(embed_dim[0])
        ###########
        # self.last_proj = nn.Linear(3456, 1728)
        self.activate = nn.ReLU()
        # self.avgpool = nn.AdaptiveAvgPool1D(1)
        # self.avgpool2d = nn.AdaptiveAvgPool2D((3, 3))
        # self.maxpool2d = nn.MaxPool2D(kernel_size=(3, 3))
        # self.head = nn.Linear(
        #     3*3*30*self.num_layers,
        #     num_classes) if self.num_classes > 0 else Identity()
        # self.converge_layers = nn.LayerList()

        # for i_layer in range(self.num_layers):
        #    conv_converge = nn.Conv2D(int(embed_dim[0] * 2 ** (self.num_layers - 1 - i_layer)), 30, kernel_size=1,
        #           stride=1, padding=0,bias_attr=False)
        #    self.converge_layers.append(conv_converge)
        self.apply(self._init_weights)
        self._freeze_stages()
        if pretrained:
            if 'http' in pretrained:  # URL
                path = paddle.utils.download.get_weights_path_from_url(
                    pretrained)
            else:  # model in local path
                path = pretrained
            self.set_state_dict(paddle.load(path))
    def _freeze_stages(self):
        if self.frozen_stages >= 0:
            self.patch_embed.eval()
            for param in self.patch_embed.parameters():
                param.stop_gradient = True

        if self.frozen_stages >= 1 and self.ape:
            self.absolute_pos_embed.stop_gradient = True

        if self.frozen_stages >= 2:
            self.pos_drop.eval()
            for i in range(0, self.frozen_stages - 1):
                m = self.layers[i]
                m.eval()
                for param in m.parameters():
                    param.stop_gradient = True

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                zeros_(m.bias)
        elif isinstance(m, nn.LayerNorm):
            zeros_(m.bias)
            ones_(m.weight)

    def forward_features(self, x):
        x = self.patch_embed(x)
        _, _, Wh, Ww = x.shape
        x = x.flatten(2).transpose([0, 2, 1])
        if self.ape:
            x = x + self.absolute_pos_embed
        x = self.pos_drop(x)
        ##############
        x_downsample = []
        #############
        for layer in self.layers:
            ############
            x_downsample.append(x)
            #############
            H, W, x, Wh, Ww = layer(x, Wh, Ww)

        x = self.norm(x)  # B L C
        return x, x_downsample, Wh, Ww
        # x = self.last_proj(x)
        # x = self.activate(x)
        # x = self.avgpool(x.transpose([0, 2, 1]))  # B C 1
        # x = paddle.flatten(x, 1)
        # return x
        # Dencoder and Skip connection
        ########################################
    def forward_up_features(self, x, x_downsample, Wh, Ww):
            x_list = []
            for inx, layer_up in enumerate(self.layers_up):
                if inx == 0:
                    output= x
                    x = layer_up(x)
                    B, L, C = output.shape
                    x_ilayer = self.activate(output)
                    #print(x.shape)
                    H = W = int(math.sqrt(L))
                    x_ilayer = x_ilayer.reshape((B, H, W, C)).transpose([0, 3, 1, 2])
                    #print(x_ilayer.shape)
                    # x_ilayer = self.avgpool2d(x_ilayer)
                    # x_ilayer = self.converge_layers[0](x_ilayer)
                    #print(x_ilayer.shape)
                    x_list.append(x_ilayer)
                else:
                    x = paddle.concat([x, x_downsample[3 - inx]], axis=-1)
                    x = self.concat_back_dim[inx](x)
                    x,output = layer_up(x, Wh, Ww)
                    x_ilayer = self.activate(output)
                    B, L, C = output.shape
                    H = W = int(math.sqrt(L))
                    x_ilayer = x_ilayer.reshape((B, H, W, C)).transpose([0, 3, 1, 2])
                    #print(x_ilayer.shape)
                    # x_ilayer = self.avgpool2d(x_ilayer)
                    # x_ilayer = self.converge_layers[inx](x_ilayer)
                    #(x_ilayer.shape)
                    x_list.append(x_ilayer)
                Wh = Wh * 2
                Ww = Ww * 2
            #x = self.norm_up(x)  # B L C

            return x_ilayer, x_list
        ########################################
    def forward(self, x):
        # forward_features(): -> [B, 1280]
        x = x['image']

        x, x_downsample, Wh, Ww = self.forward_features(x)
        x, x_list = self.forward_up_features(x, x_downsample, Wh, Ww)

        outs = []
        for i in range(self.num_layers-2, -1,-1):

            layer_x = x_list[i]

            outs.append(layer_x)


        return tuple(outs)



    @property
    def out_shape(self):
        out_strides = [4, 8, 16, 32]
        return [
            ShapeSpec(
                channels=self.num_channel[i], stride=out_strides[i])
            for i in self.out_indices
        ]



