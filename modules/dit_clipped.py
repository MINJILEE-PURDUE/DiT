import random

import torch

import torch.nn as nn
import pytorch_lightning as pl

from timm.models.vision_transformer import PatchEmbed

from modules.encoders.modules import FrozenCLIPEmbedder
from modules.utils import TimestepEmbedder, DiTBlock, FinalLayer, get_2d_sincos_pos_embed, process_input_laion


class DiT_Clipped(pl.LightningModule):
    """
    Diffusion model with a Transformer backbone and clip encoder.
    """

    def __init__(
            self,
            input_size=32,
            patch_size=2,
            in_channels=4,
            hidden_size=1152,
            context_dim=768,
            depth=28,
            num_heads=16,
            mlp_ratio=4.0,
            class_dropout_prob=0.1,
            learn_sigma=True,
            clip_version='openai/clip-vit-large-patch14'
    ):
        super().__init__()
        self.learn_sigma = learn_sigma
        self.in_channels = in_channels
        self.out_channels = in_channels * 2 if learn_sigma else in_channels
        self.patch_size = patch_size
        self.num_heads = num_heads

        self.x_embedder = PatchEmbed(input_size, patch_size, in_channels, hidden_size, bias=True)
        self.t_embedder = TimestepEmbedder(hidden_size)
        num_patches = self.x_embedder.num_patches
        # Will use fixed sin-cos embedding:
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches, hidden_size), requires_grad=False)

        self.blocks = nn.ModuleList([
            DiTBlock(hidden_size, num_heads, context_dim=context_dim, mlp_ratio=mlp_ratio) for _ in range(depth)
        ])
        self.final_layer = FinalLayer(hidden_size, patch_size, self.out_channels)

        self.encoder = FrozenCLIPEmbedder(clip_version)

        self.secondary_device = torch.device("cpu")

        self.initialize_weights()

    @torch.no_grad()
    def encode(self, text_prompt, device=None):
        device = device if device is not None else self.device
        self.encoder.to(device)
        c = self.encoder.encode(text_prompt)
        return c.to(self.device)

    def initialize_weights(self):
        # Initialize transformer layers:
        def _basic_init(module):
            if isinstance(module, nn.Linear):
                torch.nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.constant_(module.bias, 0)

        self.apply(_basic_init)

        # Initialize (and freeze) pos_embed by sin-cos embedding:
        pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1], int(self.x_embedder.num_patches ** 0.5))
        self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))

        # Initialize patch_embed like nn.Linear (instead of nn.Conv2d):
        w = self.x_embedder.proj.weight.data
        nn.init.xavier_uniform_(w.view([w.shape[0], -1]))
        nn.init.constant_(self.x_embedder.proj.bias, 0)

        # Initialize timestep embedding MLP:
        nn.init.normal_(self.t_embedder.mlp[0].weight, std=0.02)
        nn.init.normal_(self.t_embedder.mlp[2].weight, std=0.02)

        # Zero-out adaLN modulation layers in DiT blocks:
        for block in self.blocks:
            nn.init.constant_(block.adaLN_modulation[-1].weight, 0)
            nn.init.constant_(block.adaLN_modulation[-1].bias, 0)

        # Zero-out output layers:
        nn.init.constant_(self.final_layer.adaLN_modulation[-1].weight, 0)
        nn.init.constant_(self.final_layer.adaLN_modulation[-1].bias, 0)
        nn.init.constant_(self.final_layer.linear.weight, 0)
        nn.init.constant_(self.final_layer.linear.bias, 0)

    def unpatchify(self, x):
        """
        x: (N, T, patch_size**2 * C)
        imgs: (N, H, W, C)
        """
        c = self.out_channels
        p = self.x_embedder.patch_size[0]
        h = w = int(x.shape[1] ** 0.5)
        assert h * w == x.shape[1]

        x = x.reshape(shape=(x.shape[0], h, w, p, p, c))
        x = torch.einsum('nhwpqc->nchpwq', x)
        imgs = x.reshape(shape=(x.shape[0], c, h * p, h * p))
        return imgs

    def forward(self, x, t, context):
        """
        Forward pass of DiT.
        x: (N, C, H, W) tensor of spatial inputs (images or latent representations of images)
        t: (N,) tensor of diffusion timesteps
        context: (N, context_length, context_dim) embedding context
        """
        x = self.x_embedder(x) + self.pos_embed  # (N, T, D), where T = H * W / patch_size ** 2
        t = self.t_embedder(t)  # (N, D)
        for block in self.blocks:
            x = block(x, t, context)  # (N, T, D)

        # left context in, but it's not used atm
        x = self.final_layer(x, t, context)  # (N, T, patch_size ** 2 * out_channels)

        x = self.unpatchify(x)  # (N, out_channels, H, W)
        return x

    def forward_with_cfg(self, x, t, y, cfg_scale):
        """
        Forward pass of DiT, but also batches the unconditional forward pass for classifier-free guidance.
        """
        # https://github.com/openai/glide-text2im/blob/main/notebooks/text2im.ipynb
        half = x[: len(x) // 2]
        combined = torch.cat([half, half], dim=0)
        model_out = self.forward(combined, t, y)
        # For exact reproducibility reasons, we apply classifier-free guidance on only
        # three channels by default. The standard approach to cfg applies it to all channels.
        # This can be done by uncommenting the following line and commenting-out the line following that.
        # eps, rest = model_out[:, :self.in_channels], model_out[:, self.in_channels:]
        eps, rest = model_out[:, :3], model_out[:, 3:]
        cond_eps, uncond_eps = torch.split(eps, len(eps) // 2, dim=0)
        half_eps = uncond_eps + cfg_scale * (cond_eps - uncond_eps)
        eps = torch.cat([half_eps, half_eps], dim=0)
        return torch.cat([eps, rest], dim=1)

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.parameters(), lr=1e-4, weight_decay=1e-5)
        return optimizer

    def training_step(self, train_batch, batch_idx):
        img, context = train_batch

        with torch.no_grad():
            context = self.encode(context, device=self.secondary_device)
            context, img = context.to(self.device).to(self.dtype), img.to(self.secondary_device).to(torch.float32)
            self.vae.to(self.secondary_device).to(torch.float32)
            x = self.vae.encode(img).latent_dist.sample().mul_(0.18215).to(self.device).to(self.dtype)

        t = torch.randint(0, self.diffusion.num_timesteps, (x.shape[0],), device=self.device)

        # I'm paranoid
        context.requires_grad = True
        x.requires_grad = True

        model_kwargs = dict(context=context)
        loss_dict = self.diffusion.training_losses(self, x, t, model_kwargs)
        loss = loss_dict["loss"].mean()  # vb mse loss

        self.log("train_loss", loss)
        self.log("train_bv", loss_dict["vb"].mean())
        self.log("train_mse", loss_dict["mse"].mean())

        return loss

    # def validation_step(self, val_batch, batch_idx):
    #     x, y = val_batch
    #     with torch.no_grad():
    #         x = self.vae.encode(x).latent_dist.sample().mul_(0.18215)
    #     t = torch.randint(0, self.diffusion.num_timesteps, (x.shape[0],), device=self.device)
    #     model_kwargs = dict(y=y)
    #     loss_dict = self.diffusion.training_losses(self, x, t, model_kwargs)
    #     loss = loss_dict["loss"].mean()
    #     self.log("val_loss", loss)

    def backward(self, loss, optimizer, optimizer_idx, *args, **kwargs):
        loss.backward()

    def optimizer_zero_grad(self, epoch, batch_idx, optimizer, optimizer_idx):
        optimizer.zero_grad(set_to_none=True)
