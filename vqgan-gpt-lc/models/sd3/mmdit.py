### This file contains impls for MM-DiT, the core model component of SD3
import os
import math
from typing import Dict, Optional
import numpy as np
import torch
import torch.nn as nn
from einops import rearrange, repeat
from models.sd3.other_impls import attention, Mlp

import torch.distributed as dist


class PatchEmbed(nn.Module):
    """2D Image to Patch Embedding"""

    def __init__(
        self,
        img_size: Optional[int] = 224,
        patch_size: int = 16,
        in_chans: int = 3,
        embed_dim: int = 768,
        flatten: bool = True,
        bias: bool = True,
        strict_img_size: bool = True,
        dynamic_img_pad: bool = False,
        dtype=None,
        device=None,
    ):
        super().__init__()
        self.patch_size = (patch_size, patch_size)
        if img_size is not None:
            self.img_size = (img_size, img_size)
            self.grid_size = tuple([s // p for s, p in zip(self.img_size, self.patch_size)])
            self.num_patches = self.grid_size[0] * self.grid_size[1]
        else:
            self.img_size = None
            self.grid_size = None
            self.num_patches = None

        # flatten spatial dim and transpose to channels last, kept for bwd compat
        self.flatten = flatten
        self.strict_img_size = strict_img_size
        self.dynamic_img_pad = dynamic_img_pad

        self.proj = nn.Conv2d(
            in_chans, embed_dim, kernel_size=patch_size, stride=patch_size, bias=bias, dtype=dtype, device=device
        )

    def forward(self, x):
        B, C, H, W = x.shape
        x = self.proj(x)
        if self.flatten:
            x = x.flatten(2).transpose(1, 2)  # NCHW -> NLC
        return x


def modulate(x, shift, scale):
    if shift is None:
        shift = torch.zeros_like(scale)
    return x * (1 + scale.unsqueeze(1)) + shift.unsqueeze(1)


#################################################################################
#                   Sine/Cosine Positional Embedding Functions                  #
#################################################################################


def get_2d_sincos_pos_embed(embed_dim, grid_size, cls_token=False, extra_tokens=0, scaling_factor=None, offset=None):
    """
    grid_size: int of the grid height and width
    return:
    pos_embed: [grid_size*grid_size, embed_dim] or [1+grid_size*grid_size, embed_dim] (w/ or w/o cls_token)
    """
    grid_h = np.arange(grid_size, dtype=np.float32)
    grid_w = np.arange(grid_size, dtype=np.float32)
    grid = np.meshgrid(grid_w, grid_h)  # here w goes first
    grid = np.stack(grid, axis=0)
    if scaling_factor is not None:
        grid = grid / scaling_factor
    if offset is not None:
        grid = grid - offset
    grid = grid.reshape([2, 1, grid_size, grid_size])
    pos_embed = get_2d_sincos_pos_embed_from_grid(embed_dim, grid)
    if cls_token and extra_tokens > 0:
        pos_embed = np.concatenate([np.zeros([extra_tokens, embed_dim]), pos_embed], axis=0)
    return pos_embed


def get_2d_sincos_pos_embed_from_grid(embed_dim, grid):
    assert embed_dim % 2 == 0
    # use half of dimensions to encode grid_h
    emb_h = get_1d_sincos_pos_embed_from_grid(embed_dim // 2, grid[0])  # (H*W, D/2)
    emb_w = get_1d_sincos_pos_embed_from_grid(embed_dim // 2, grid[1])  # (H*W, D/2)
    emb = np.concatenate([emb_h, emb_w], axis=1)  # (H*W, D)
    return emb


def get_1d_sincos_pos_embed_from_grid(embed_dim, pos):
    """
    embed_dim: output dimension for each position
    pos: a list of positions to be encoded: size (M,)
    out: (M, D)
    """
    assert embed_dim % 2 == 0
    omega = np.arange(embed_dim // 2, dtype=np.float64)
    omega /= embed_dim / 2.0
    omega = 1.0 / 10000**omega  # (D/2,)
    pos = pos.reshape(-1)  # (M,)
    out = np.einsum("m,d->md", pos, omega)  # (M, D/2), outer product
    emb_sin = np.sin(out)  # (M, D/2)
    emb_cos = np.cos(out)  # (M, D/2)
    return np.concatenate([emb_sin, emb_cos], axis=1)  # (M, D)


#################################################################################
#               Embedding Layers for Timesteps and Class Labels                 #
#################################################################################


class TimestepEmbedder(nn.Module):
    """Embeds scalar timesteps into vector representations."""

    def __init__(self, hidden_size, frequency_embedding_size=256, dtype=None, device=None):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(frequency_embedding_size, hidden_size, bias=True, dtype=dtype, device=device),
            nn.SiLU(),
            nn.Linear(hidden_size, hidden_size, bias=True, dtype=dtype, device=device),
        )
        self.frequency_embedding_size = frequency_embedding_size

    @staticmethod
    def timestep_embedding(t, dim, max_period=10000):
        """
        Create sinusoidal timestep embeddings.
        :param t: a 1-D Tensor of N indices, one per batch element.
                          These may be fractional.
        :param dim: the dimension of the output.
        :param max_period: controls the minimum frequency of the embeddings.
        :return: an (N, D) Tensor of positional embeddings.
        """
        half = dim // 2
        freqs = torch.exp(-math.log(max_period) * torch.arange(start=0, end=half, dtype=torch.float32) / half).to(
            device=t.device
        )
        args = t[:, None].float() * freqs[None]
        embedding = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)
        if dim % 2:
            embedding = torch.cat([embedding, torch.zeros_like(embedding[:, :1])], dim=-1)
        if torch.is_floating_point(t):
            embedding = embedding.to(dtype=t.dtype)
        return embedding

    def forward(self, t, dtype, **kwargs):
        t_freq = self.timestep_embedding(t, self.frequency_embedding_size).to(dtype)
        t_emb = self.mlp(t_freq)
        return t_emb


class VectorEmbedder(nn.Module):
    """Embeds a flat vector of dimension input_dim"""

    def __init__(self, input_dim: int, hidden_size: int, dtype=None, device=None):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(input_dim, hidden_size, bias=True, dtype=dtype, device=device),
            nn.SiLU(),
            nn.Linear(hidden_size, hidden_size, bias=True, dtype=dtype, device=device),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.mlp(x)


class LabelEmbedder(nn.Module):
    """
    Embeds class labels into vector representations. Also handles label dropout for classifier-free guidance.
    """
    def __init__(self, num_classes, hidden_size, dropout_prob, dtype=None):
        super().__init__()
        use_cfg_embedding = dropout_prob > 0
        self.embedding_table = nn.Embedding(num_classes + use_cfg_embedding, hidden_size, dtype=dtype)
        self.num_classes = num_classes
        self.dropout_prob = dropout_prob

    def token_drop(self, labels, force_drop_ids=None):
        """
        Drops labels to enable classifier-free guidance.
        """
        if force_drop_ids is None:
            drop_ids = torch.rand(labels.shape[0], device=labels.device) < self.dropout_prob
        else:
            drop_ids = force_drop_ids == 1
        labels = torch.where(drop_ids, self.num_classes, labels)
        return labels

    def forward(self, labels, train, dtype, force_drop_ids=None):
        use_dropout = self.dropout_prob > 0
        if (train and use_dropout) or (force_drop_ids is not None):
            labels = self.token_drop(labels, force_drop_ids)
        embeddings = self.embedding_table(labels).to(dtype)
        return embeddings


#################################################################################
#                                 Core DiT Model                                #
#################################################################################


def split_qkv(qkv, head_dim):
    qkv = qkv.reshape(qkv.shape[0], qkv.shape[1], 3, -1, head_dim).movedim(2, 0)
    return qkv[0], qkv[1], qkv[2]


def optimized_attention(qkv, num_heads):
    return attention(qkv[0], qkv[1], qkv[2], num_heads)


class SelfAttention(nn.Module):
    ATTENTION_MODES = ("xformers", "torch", "torch-hb", "math", "debug")

    def __init__(
        self,
        dim: int,
        num_heads: int = 8,
        qkv_bias: bool = False,
        qk_scale: Optional[float] = None,
        attn_mode: str = "xformers",
        pre_only: bool = False,
        qk_norm: Optional[str] = None,
        rmsnorm: bool = False,
        dtype=None,
        device=None,
    ):
        super().__init__()
        self.num_heads = num_heads
        self.head_dim = dim // num_heads

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias, dtype=dtype, device=device)
        if not pre_only:
            self.proj = nn.Linear(dim, dim, dtype=dtype, device=device)
        assert attn_mode in self.ATTENTION_MODES
        self.attn_mode = attn_mode
        self.pre_only = pre_only

        if qk_norm == "rms":
            self.ln_q = RMSNorm(self.head_dim, elementwise_affine=True, eps=1.0e-6, dtype=dtype, device=device)
            self.ln_k = RMSNorm(self.head_dim, elementwise_affine=True, eps=1.0e-6, dtype=dtype, device=device)
        elif qk_norm == "ln":
            self.ln_q = nn.LayerNorm(self.head_dim, elementwise_affine=True, eps=1.0e-6, dtype=dtype, device=device)
            self.ln_k = nn.LayerNorm(self.head_dim, elementwise_affine=True, eps=1.0e-6, dtype=dtype, device=device)
        elif qk_norm is None:
            self.ln_q = nn.Identity()
            self.ln_k = nn.Identity()
        else:
            raise ValueError(qk_norm)

    def pre_attention(self, x: torch.Tensor):
        B, L, C = x.shape
        qkv = self.qkv(x)
        q, k, v = split_qkv(qkv, self.head_dim)
        q = self.ln_q(q).reshape(q.shape[0], q.shape[1], -1)
        k = self.ln_k(k).reshape(q.shape[0], q.shape[1], -1)
        return (q, k, v)

    def post_attention(self, x: torch.Tensor) -> torch.Tensor:
        assert not self.pre_only
        x = self.proj(x)
        return x

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        (q, k, v) = self.pre_attention(x)
        x = attention(q, k, v, self.num_heads)
        x = self.post_attention(x)
        return x


class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, elementwise_affine: bool = False, eps: float = 1e-6, device=None, dtype=None):
        """
        Initialize the RMSNorm normalization layer.
        Args:
            dim (int): The dimension of the input tensor.
            eps (float, optional): A small value added to the denominator for numerical stability. Default is 1e-6.
        Attributes:
            eps (float): A small value added to the denominator for numerical stability.
            weight (nn.Parameter): Learnable scaling parameter.
        """
        super().__init__()
        self.eps = eps
        self.learnable_scale = elementwise_affine
        if self.learnable_scale:
            self.weight = nn.Parameter(torch.empty(dim, device=device, dtype=dtype))
        else:
            self.register_parameter("weight", None)

    def _norm(self, x):
        """
        Apply the RMSNorm normalization to the input tensor.
        Args:
            x (torch.Tensor): The input tensor.
        Returns:
            torch.Tensor: The normalized tensor.
        """
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        """
        Forward pass through the RMSNorm layer.
        Args:
            x (torch.Tensor): The input tensor.
        Returns:
            torch.Tensor: The output tensor after applying RMSNorm.
        """
        x = self._norm(x)
        if self.learnable_scale:
            return x * self.weight.to(device=x.device, dtype=x.dtype)
        else:
            return x


class SwiGLUFeedForward(nn.Module):
    def __init__(
        self,
        dim: int,
        hidden_dim: int,
        multiple_of: int,
        ffn_dim_multiplier: Optional[float] = None,
    ):
        """
        Initialize the FeedForward module.

        Args:
            dim (int): Input dimension.
            hidden_dim (int): Hidden dimension of the feedforward layer.
            multiple_of (int): Value to ensure hidden dimension is a multiple of this value.
            ffn_dim_multiplier (float, optional): Custom multiplier for hidden dimension. Defaults to None.

        Attributes:
            w1 (ColumnParallelLinear): Linear transformation for the first layer.
            w2 (RowParallelLinear): Linear transformation for the second layer.
            w3 (ColumnParallelLinear): Linear transformation for the third layer.

        """
        super().__init__()
        hidden_dim = int(2 * hidden_dim / 3)
        # custom dim factor multiplier
        if ffn_dim_multiplier is not None:
            hidden_dim = int(ffn_dim_multiplier * hidden_dim)
        hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        self.w1 = nn.Linear(dim, hidden_dim, bias=False)
        self.w2 = nn.Linear(hidden_dim, dim, bias=False)
        self.w3 = nn.Linear(dim, hidden_dim, bias=False)

    def forward(self, x):
        return self.w2(nn.functional.silu(self.w1(x)) * self.w3(x))


class DismantledBlock(nn.Module):
    """A DiT block with gated adaptive layer norm (adaLN) conditioning."""

    ATTENTION_MODES = ("xformers", "torch", "torch-hb", "math", "debug")

    def __init__(
        self,
        hidden_size: int,
        num_heads: int,
        mlp_ratio: float = 4.0,
        attn_mode: str = "xformers",
        qkv_bias: bool = False,
        pre_only: bool = False,
        rmsnorm: bool = False,
        scale_mod_only: bool = False,
        swiglu: bool = False,
        qk_norm: Optional[str] = None,
        dtype=None,
        device=None,
        **block_kwargs,
    ):
        super().__init__()
        assert attn_mode in self.ATTENTION_MODES
        if not rmsnorm:
            self.norm1 = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6, dtype=dtype, device=device)
        else:
            self.norm1 = RMSNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        self.attn = SelfAttention(
            dim=hidden_size,
            num_heads=num_heads,
            qkv_bias=qkv_bias,
            attn_mode=attn_mode,
            pre_only=pre_only,
            qk_norm=qk_norm,
            rmsnorm=rmsnorm,
            dtype=dtype,
            device=device,
        )
        if not pre_only:
            if not rmsnorm:
                self.norm2 = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6, dtype=dtype, device=device)
            else:
                self.norm2 = RMSNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        mlp_hidden_dim = int(hidden_size * mlp_ratio)
        if not pre_only:
            if not swiglu:
                self.mlp = Mlp(
                    in_features=hidden_size,
                    hidden_features=mlp_hidden_dim,
                    act_layer=nn.GELU(approximate="tanh"),
                    dtype=dtype,
                    device=device,
                )
                # self.mlp = Mlp(in_features=hidden_size, hidden_features=mlp_hidden_dim, act_layer=nn.GELU(), dtype=dtype, device=device)
            else:
                self.mlp = SwiGLUFeedForward(dim=hidden_size, hidden_dim=mlp_hidden_dim, multiple_of=256)
        self.scale_mod_only = scale_mod_only
        if not scale_mod_only:
            n_mods = 6 if not pre_only else 2
        else:
            n_mods = 4 if not pre_only else 1
        self.adaLN_modulation = nn.Sequential(
            nn.SiLU(), nn.Linear(hidden_size, n_mods * hidden_size, bias=True, dtype=dtype, device=device)
        )
        self.pre_only = pre_only

    def pre_attention(self, x: torch.Tensor, c: torch.Tensor):
        assert x is not None, "pre_attention called with None input"
        if not self.pre_only:
            if not self.scale_mod_only:
                shift_msa, scale_msa, gate_msa, shift_mlp, scale_mlp, gate_mlp = self.adaLN_modulation(c).chunk(
                    6, dim=1
                )
            else:
                shift_msa = None
                shift_mlp = None
                scale_msa, gate_msa, scale_mlp, gate_mlp = self.adaLN_modulation(c).chunk(4, dim=1)
            qkv = self.attn.pre_attention(modulate(self.norm1(x), shift_msa, scale_msa))
            return qkv, (x, gate_msa, shift_mlp, scale_mlp, gate_mlp)
        else:
            if not self.scale_mod_only:
                shift_msa, scale_msa = self.adaLN_modulation(c).chunk(2, dim=1)
            else:
                shift_msa = None
                scale_msa = self.adaLN_modulation(c)
            qkv = self.attn.pre_attention(modulate(self.norm1(x), shift_msa, scale_msa))
            return qkv, None

    def post_attention(self, attn, x, gate_msa, shift_mlp, scale_mlp, gate_mlp):
        assert not self.pre_only
        x = x + gate_msa.unsqueeze(1) * self.attn.post_attention(attn)
        x = x + gate_mlp.unsqueeze(1) * self.mlp(modulate(self.norm2(x), shift_mlp, scale_mlp))
        return x

    def forward(self, x: torch.Tensor, c: torch.Tensor) -> torch.Tensor:
        assert not self.pre_only
        (q, k, v), intermediates = self.pre_attention(x, c)
        attn = attention(q, k, v, self.attn.num_heads)
        return self.post_attention(attn, *intermediates)


def block_mixing(context, x, context_block, x_block, c, mask=None):
    assert context is not None, "block_mixing called with None context"
    context_qkv, context_intermediates = context_block.pre_attention(context, c)

    x_qkv, x_intermediates = x_block.pre_attention(x, c)

    o = []
    for t in range(3):
        o.append(torch.cat((context_qkv[t], x_qkv[t]), dim=1))
    q, k, v = tuple(o)

    attn = attention(q, k, v, x_block.attn.num_heads, mask)
    #print('mask',mask[0])
    context_attn, x_attn = (attn[:, : context_qkv[0].shape[1]], attn[:, context_qkv[0].shape[1] :])

    if not context_block.pre_only:
        context = context_block.post_attention(context_attn, *context_intermediates)
    else:
        context = None
    x = x_block.post_attention(x_attn, *x_intermediates)
    return context, x


class JointBlock(nn.Module):
    """just a small wrapper to serve as a fsdp unit"""

    def __init__(self, use_checkpoint, *args, **kwargs):
        super().__init__()
        pre_only = kwargs.pop("pre_only")
        qk_norm = kwargs.pop("qk_norm", None)
        self.use_checkpoint = use_checkpoint
        self.context_block = DismantledBlock(*args, pre_only=pre_only, qk_norm=qk_norm, **kwargs)
        self.x_block = DismantledBlock(*args, pre_only=False, qk_norm=qk_norm, **kwargs)

    def _forward(self, context, x, c, mask=None):
        return block_mixing(context, x, context_block=self.context_block, x_block=self.x_block, c=c, mask=mask)

    def forward(self, context, x, c, mask=None):
        if self.use_checkpoint:
            return torch.utils.checkpoint.checkpoint(self._forward, context, x, c, mask, use_reentrant=False)
        else:
            return _forward(context, x, c, mask)


class FinalLayer(nn.Module):
    """
    The final layer of DiT.
    """

    def __init__(
        self,
        hidden_size: int,
        patch_size: int,
        out_channels: int,
        total_out_channels: Optional[int] = None,
        dtype=None,
        device=None,
    ):
        super().__init__()
        self.norm_final = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6, dtype=dtype, device=device)
        self.linear = (
            nn.Linear(hidden_size, patch_size * patch_size * out_channels, bias=True, dtype=dtype, device=device)
            if (total_out_channels is None)
            else nn.Linear(hidden_size, total_out_channels, bias=True, dtype=dtype, device=device)
        )
        self.adaLN_modulation = nn.Sequential(
            nn.SiLU(), nn.Linear(hidden_size, 2 * hidden_size, bias=True, dtype=dtype, device=device)
        )

    def forward(self, x: torch.Tensor, c: torch.Tensor) -> torch.Tensor:
        shift, scale = self.adaLN_modulation(c).chunk(2, dim=1)
        x = modulate(self.norm_final(x), shift, scale)
        x = self.linear(x)
        return x


class MMDiT(nn.Module):
    """Diffusion model with a Transformer backbone."""

    def __init__(
        self,
        input_size: int = 32,
        patch_size: int = 2,
        in_channels: int = 4,
        depth: int = 28,
        mlp_ratio: float = 4.0,
        class_dropout_prob: float = 0.1,
        learn_sigma: bool = False,
        adm_in_channels: Optional[int] = None,
        context_embedder_config: Optional[Dict] = None,
        register_length: int = 0,
        attn_mode: str = "torch",
        rmsnorm: bool = False,
        scale_mod_only: bool = False,
        swiglu: bool = False,
        out_channels: Optional[int] = None,
        pos_embed_scaling_factor: Optional[float] = None,
        pos_embed_offset: Optional[float] = None,
        pos_embed_max_size: Optional[int] = None,
        num_patches=None,
        qk_norm: Optional[str] = None,
        qkv_bias: bool = True,
        dtype=None,
        device=None,
        train_filter=["attn", "adaLN_modulation", "context_embedder", "mlp"],
        freeze_filter=[],
        use_checkpoint=True,
        sd3_cond_pooling=None,
        uncond_c_file='./selftok/uncond_c_after.pt', 
        uncond_y_file='./selftok/uncond_y_after.pt',
        **kwargs
    ):
        super().__init__()
        print(
            f"mmdit initializing with: {input_size=}, {patch_size=}, {in_channels=}, {depth=}, {mlp_ratio=}, {learn_sigma=}, {adm_in_channels=}, {context_embedder_config=}, {register_length=}, {attn_mode=}, {rmsnorm=}, {scale_mod_only=}, {swiglu=}, {out_channels=}, {pos_embed_scaling_factor=}, {pos_embed_offset=}, {pos_embed_max_size=}, {num_patches=}, {qk_norm=}, {qkv_bias=}, {dtype=}, {device=}"
        )
        self.dtype = dtype
        self.learn_sigma = learn_sigma
        self.in_channels = in_channels
        default_out_channels = in_channels * 2 if learn_sigma else in_channels
        self.out_channels = out_channels if out_channels is not None else default_out_channels
        self.patch_size = patch_size
        self.pos_embed_scaling_factor = pos_embed_scaling_factor
        self.pos_embed_offset = pos_embed_offset
        self.pos_embed_max_size = pos_embed_max_size
        self.train_filter = train_filter
        self.freeze_filter = freeze_filter
        self.sd3_cond_pooling = sd3_cond_pooling
        self.class_dropout_prob = class_dropout_prob
        # apply magic --> this defines a head_size of 64
        hidden_size = 64 * depth
        num_heads = depth

        self.num_heads = num_heads

        self.x_embedder = PatchEmbed(
            img_size=None,
            patch_size=patch_size,
            in_chans=in_channels,
            embed_dim=hidden_size,
            bias=True,
            strict_img_size=self.pos_embed_max_size is None,
            dtype=dtype,
            device=device,
        )
        self.t_embedder = TimestepEmbedder(hidden_size, dtype=dtype, device=device)

        if adm_in_channels is not None:
            assert isinstance(adm_in_channels, int)
            self.y_embedder = VectorEmbedder(adm_in_channels, hidden_size, dtype=dtype, device=device)
        else:
            self.y_embedder = LabelEmbedder(1000, hidden_size, class_dropout_prob, dtype=dtype)


        self.context_embedder = nn.Identity()
        if context_embedder_config is not None:
            if context_embedder_config["target"] == "torch.nn.Linear":
                self.context_embedder = nn.Linear(**context_embedder_config["params"], dtype=dtype, device=device)

        self.register_length = register_length
        if self.register_length > 0:
            self.register = nn.Parameter(torch.randn(1, register_length, hidden_size, dtype=dtype, device=device))

        # num_patches = self.x_embedder.num_patches
        # Will use fixed sin-cos embedding:
        # just use a buffer already
        if num_patches is not None:
            self.register_buffer(
                "pos_embed",
                torch.zeros(1, num_patches, hidden_size, dtype=dtype, device=device),
            )
        else:
            self.pos_embed = None

        self.joint_blocks = nn.ModuleList(
            [
                JointBlock(
                    use_checkpoint,
                    hidden_size,
                    num_heads,
                    mlp_ratio=mlp_ratio,
                    qkv_bias=qkv_bias,
                    attn_mode=attn_mode,
                    pre_only=i == depth - 1,
                    rmsnorm=rmsnorm,
                    scale_mod_only=scale_mod_only,
                    swiglu=swiglu,
                    qk_norm=qk_norm,
                    dtype=dtype,
                    device=device,
                )
                for i in range(depth)
            ]
        )
        self.final_layer = FinalLayer(hidden_size, patch_size, self.out_channels, dtype=dtype, device=device)
        # context pos embed
        assert "K" in kwargs, "Number of tokens not specified"
        context_len = kwargs.pop("K", 680)
        context_pos_embed_hidden_size = context_embedder_config["params"]["out_features"]
        self.register_buffer(
            "context_pos_embed",
            torch.zeros(1, context_len, context_pos_embed_hidden_size)
        )
        context_pos_embed = get_1d_sincos_pos_embed_from_grid(
            self.context_pos_embed.shape[-1],
            np.arange(context_len, dtype=np.float32)
        )
        
        self.context_pos_embed.data.copy_(torch.from_numpy(context_pos_embed).float().unsqueeze(0))

        # load uncond embedding
        if uncond_c_file is not None:
            self.uncond_c = torch.load(uncond_c_file, map_location='cpu').to(device)
            self.uncond_c.requires_grad = False
        if uncond_y_file is not None:
            self.uncond_y = torch.load(uncond_y_file, map_location='cpu').to(device)
            self.uncond_y.requires_grad = False

    def freeze(self):
        freezed_param_names = []
        train_param_names = []
        for name, param in self.named_parameters():
            if self.train_filter is not None:
                # if not train all
                if any(item in name for item in self.train_filter) and \
                    not any(item in name for item in self.freeze_filter):
                    param.requires_grad = True
                    train_param_names.append(name)
                else:
                    param.requires_grad = False
                    freezed_param_names.append(name)
            elif not any(item in name for item in self.freeze_filter):
                param.requires_grad = True
                train_param_names.append(name)
            else:
                param.requires_grad = False
                freezed_param_names.append(name)
        return train_param_names, freezed_param_names
    
    def parameters(self, recurse=True):
        """
        Override the parameters() function to yield only parameters
        whose names contain 'cross_modulation'.

        :param recurse: Whether to recurse into submodules (default: True)
        :param self.train_filter: The string to filter parameter names by (default: 'cross_modulation')
        :return: An iterator over module parameters matching the filter
        """
        for name, param in self.named_parameters(recurse=recurse):
            if self.train_filter is not None:
                if any(item in name for item in self.train_filter) and \
                    not any(item in name for item in self.freeze_filter):
                # 't_embedder.mlp' not in name:
                    yield param
            elif not any(item in name for item in self.freeze_filter):
                yield param

    def get_params_by_filter(self, select_list=None, remove_list=[]):
        remove_list += self.freeze_filter
        for name, param in self.named_parameters(recurse=True):
            if select_list is not None:
                if any(item in name for item in select_list) and \
                    not any(item in name for item in remove_list):
                    yield param
            elif not any(item in name for item in remove_list):
                yield param

    def cropped_pos_embed(self, hw):
        assert self.pos_embed_max_size is not None
        p = self.x_embedder.patch_size[0]
        h, w = hw
        # patched size
        h = h // p
        w = w // p
        assert h <= self.pos_embed_max_size, (h, self.pos_embed_max_size)
        assert w <= self.pos_embed_max_size, (w, self.pos_embed_max_size)
        top = (self.pos_embed_max_size - h) // 2
        left = (self.pos_embed_max_size - w) // 2
        spatial_pos_embed = rearrange(
            self.pos_embed,
            "1 (h w) c -> 1 h w c",
            h=self.pos_embed_max_size,
            w=self.pos_embed_max_size,
        )
        spatial_pos_embed = spatial_pos_embed[:, top : top + h, left : left + w, :]
        spatial_pos_embed = rearrange(spatial_pos_embed, "1 h w c -> 1 (h w) c")
        return spatial_pos_embed

    def unpatchify(self, x, hw=None):
        """
        x: (N, T, patch_size**2 * C)
        imgs: (N, H, W, C)
        """
        c = self.out_channels
        p = self.x_embedder.patch_size[0]
        if hw is None:
            h = w = int(x.shape[1] ** 0.5)
        else:
            h, w = hw
            h = h // p
            w = w // p
        assert h * w == x.shape[1]

        x = x.reshape(shape=(x.shape[0], h, w, p, p, c))
        x = torch.einsum("nhwpqc->nchpwq", x)
        imgs = x.reshape(shape=(x.shape[0], c, h * p, w * p))
        return imgs

    def forward_core_with_concat(self, x, c_mod, context=None, mask=None):
        if self.register_length > 0:
            context = torch.cat(
                (
                    repeat(self.register, "1 ... -> b ...", b=x.shape[0]),
                    context if context is not None else torch.Tensor([]).type_as(x),
                ),
                1,
            )

        # context is B, L', D
        # x is B, L, D
        for block in self.joint_blocks:
            context, x = block(context, x, c=c_mod, mask=mask)

        x = self.final_layer(x, c_mod)  # (N, T, patch_size ** 2 * out_channels)
        return x

    def drop_cond(self, context, y, mask):
        if self.class_dropout_prob <= 0.0 or (not self.training):
            return context, mask, y
        # with torch.no_grad():
        drop_ids = torch.rand(
            context.shape[0], device=context.device
        ) < self.class_dropout_prob
        seq_len = self.uncond_c.size(1)
        
        drop_mask = torch.zeros(context.size(), dtype=torch.int, device=context.device)
        uncond_c = torch.zeros(context.size(), dtype=context.dtype, device=context.device)
        uncond_c[drop_ids, :seq_len, :] = self.uncond_c.to(context.dtype).to(context.device)
        drop_mask[drop_ids, :seq_len, :] = 1
        context = (1-drop_mask) * context + drop_mask * uncond_c 
        # print('before',torch.sum(mask[drop_ids, :],dim=1),mask[drop_ids, 152:155], mask.dtype)
        if mask is not None:
            drop_mask = torch.zeros(mask.size(), dtype=torch.int, device=mask.device)
            uncond_mask = torch.zeros(mask.size(), dtype=mask.dtype, device=mask.device)
            drop_mask[drop_ids, :] = 1
            uncond_mask[drop_ids, :seq_len] = 1
            uncond_mask[drop_ids, seq_len:] = 0
            mask = (1-drop_mask) * mask + drop_mask * uncond_mask
        # print('after',torch.sum(mask[drop_ids, :],dim=1),mask[drop_ids, 152:155], mask.dtype)
        if y is not None:
            drop_mask = torch.zeros(y.size(), dtype=torch.int, device=y.device)
            uncond_y = torch.zeros(y.size(), dtype=y.dtype, device=y.device)
            uncond_y[drop_ids, :] = self.uncond_y.to(y.dtype).to(y.device)
            drop_mask[drop_ids, :] = 1
            y = (1-drop_mask) * y + drop_mask * uncond_y
            # print(y.size(), uncond_y.size(), self.uncond_y.size())
            # y[drop_ids, :] = self.uncond_y.to(y.dtype).to(y.device)
        return context, mask, y

    def forward(self, x, t, y=None, encoder_hidden_states=None, x_mask=None, mask=None,**kwargs):
        """
        Forward pass of DiT.
        x: (N, C, H, W) tensor of spatial inputs (images or latent representations of images)
        t: (N,) tensor of diffusion timesteps
        y: (N,) tensor of class labels
        """
        hw = x.shape[-2:]
        t = torch.floor(t * 1000).int().clamp(0, 999)
        x = self.x_embedder(x) + self.cropped_pos_embed(hw)
        c = self.t_embedder(t, dtype=x.dtype)  # (N, D)

        if self.sd3_cond_pooling == 'last':
            k_batch = mask.sum(dim=-1) - 1
            y = encoder_hidden_states[torch.arange(x.shape[0]), k_batch, :]
        elif self.sd3_cond_pooling == 'mean':
            y = encoder_hidden_states.sum(dim=1) / mask.sum(dim=-1).unsqueeze(1)

        if y is not None:
            # y = self.y_embedder(y,self.training, dtype=x.dtype)  # (N, D)
            y = self.y_embedder(y)
        context = self.context_embedder(encoder_hidden_states).to(x.dtype) + self.context_pos_embed
        context, mask, y = self.drop_cond(context, y, mask)

        if y is not None:
            c = c + y  # (N, D)

        if x_mask is None:
            x_mask = torch.ones((x.shape[0], x.shape[1])).cuda()
        if mask is None:
            mask = torch.ones((context.shape[0], context.shape[1])).cuda()

        mask = torch.cat((mask, x_mask), dim=1).bool()
        mask = mask.unsqueeze(1).unsqueeze(2).repeat(1,1,context.shape[1]+x.shape[1],1)

        x = self.forward_core_with_concat(x, c, context, mask=mask)
        x = self.unpatchify(x, hw=hw)  # (N, out_channels, H, W)
        return x

