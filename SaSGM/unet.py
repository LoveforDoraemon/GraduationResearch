import layers, normalization, utils, attention
import torch.nn as nn
import functools
import torch

ResnetBlockDDPM = layers.ResnetBlockDDPMpp
ResnetBlockBigGAN = layers.ResnetBlockBigGANpp
AttnBlock = attention.SpatialTransformer  # NOTE
# Combine = layers.Combine
conv3x3 = layers.conv3x3
conv1x1 = layers.conv1x1
get_act = layers.get_act
get_normalization = normalization.get_normalization
default_initializer = layers.default_init


class UNet(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.act = act = get_act(config)  # 选择激活函数
        # 缓冲区张量sigmas，不被更新
        self.register_buffer("sigmas", torch.tensor(utils.get_sigmas(config)))

        self.nf = nf = config.model.nf  # 模型基础通道数 = 128
        ch_mult = config.model.ch_mult  # 下采样过程的通道倍增系数
        self.num_res_blocks = num_res_blocks = (
            config.model.num_res_blocks
        )  # 第一个config中是2
        dropout = config.model.dropout  # dropout rate
        resamp_with_conv = config.model.resamp_with_conv

        self.num_resolutions = num_resolutions = len(ch_mult)  # default = 6
        # 一系列分辨率值 for max_res_num = 128 all_resolutions = [128,64,32,16,8,4]
        # self.all_resolutions = all_resolutions = [
        #     config.data.max_res_num // (2**i) for i in range(num_resolutions)
        # ]

        self.skip_rescale = skip_rescale = config.model.skip_rescale
        self.resblock_type = resblock_type = (
            config.model.resblock_type.lower()
        )  # biggan for inpaint
        init_scale = config.model.init_scale

        self.embedding_type = embedding_type = config.model.embedding_type.lower()

        assert embedding_type in ["fourier", "positional"]

        modules = []
        embed_dim = nf

        # process time_cond
        modules.append(nn.Linear(embed_dim, nf * 4))
        modules[-1].weight.data = default_initializer()(modules[-1].weight.shape)
        nn.init.zeros_(modules[-1].bias)

        modules.append(nn.Linear(nf * 4, nf * 4))
        modules[-1].weight.data = default_initializer()(modules[-1].weight.shape)
        nn.init.zeros_(modules[-1].bias)

        Upsample = functools.partial(layers.Upsample, with_conv=resamp_with_conv)

        Downsample = functools.partial(layers.Downsample, with_conv=resamp_with_conv)

        if resblock_type == "ddpm":
            ResnetBlock = functools.partial(
                ResnetBlockDDPM,
                act=act,
                dropout=dropout,
                init_scale=init_scale,
                skip_rescale=skip_rescale,
                temb_dim=nf * 4,
            )

        elif resblock_type == "biggan":
            ResnetBlock = functools.partial(
                ResnetBlockBigGAN,
                act=act,
                dropout=dropout,
                init_scale=init_scale,
                skip_rescale=skip_rescale,
                temb_dim=nf * 4,
            )

        else:
            raise ValueError(f"resblock type {resblock_type} unrecognized.")

        # Downsampling block
        channels = (
            config.data.num_channels
        )  # 5 dist,omega,theta,phi+[block_adj]+padding
        modules.append(conv3x3(channels, nf))
        hs_c = [nf]

        in_ch = nf
        for i_level in range(num_resolutions):  # default = 6
            # Residual blocks for this resolution
            for i_block in range(num_res_blocks):  # default = 2
                out_ch = nf * ch_mult[i_level]  # ch_mult = [1,1,2,2,2,2]
                modules.append(ResnetBlock(in_ch=in_ch, out_ch=out_ch))
                in_ch = out_ch

                # NOTE Attention added here
                modules.append(AttnBlock(in_channels=in_ch, n_heads=4, d_head=32))
                hs_c.append(in_ch)

            if i_level != num_resolutions - 1:
                if resblock_type == "ddpm":
                    modules.append(Downsample(in_ch=in_ch))
                else:
                    modules.append(ResnetBlock(down=True, in_ch=in_ch))
                hs_c.append(in_ch)

        in_ch = hs_c[-1]
        modules.append(ResnetBlock(in_ch=in_ch))
        modules.append(AttnBlock(in_channels=in_ch, n_heads=4, d_head=32))  # NOTE
        modules.append(ResnetBlock(in_ch=in_ch))

        # Upsampling block
        for i_level in reversed(range(num_resolutions)):  # 生成反向迭代器
            for i_block in range(num_res_blocks + 1):
                out_ch = nf * ch_mult[i_level]
                modules.append(ResnetBlock(in_ch=in_ch + hs_c.pop(), out_ch=out_ch))
                in_ch = out_ch

                # NOTE attention added here
                modules.append(AttnBlock(in_channels=in_ch, n_heads=4, d_head=32))

            if i_level != 0:
                if resblock_type == "ddpm":
                    modules.append(Upsample(in_ch=in_ch))
                else:
                    modules.append(ResnetBlock(in_ch=in_ch, up=True))

        assert not hs_c

        modules.append(
            nn.GroupNorm(num_groups=min(in_ch // 4, 32), num_channels=in_ch, eps=1e-6)
        )
        modules.append(conv3x3(in_ch, channels, init_scale=init_scale))

        self.all_modules = nn.ModuleList(modules)

    def forward(self, x, time_cond, context):
        modules = self.all_modules
        m_idx = 0
        # Sinusoidal positional embeddings.
        timesteps = time_cond
        used_sigmas = self.sigmas[time_cond.long()]
        temb = layers.get_timestep_embedding(timesteps, self.nf)

        temb = modules[m_idx](temb)  # Linear 128,256
        m_idx += 1
        temb = modules[m_idx](self.act(temb))  # Linear 256,256
        m_idx += 1

        # Downsampling block
        hs = [modules[m_idx](x)]
        m_idx += 1
        for i_level in range(self.num_resolutions):
            # Residual blocks for this resolution
            for i_block in range(self.num_res_blocks):
                h = modules[m_idx](hs[-1], temb)
                m_idx += 1

                h = modules[m_idx](h, context)  # Spatial Transformer
                m_idx += 1

                hs.append(h)

            if i_level != self.num_resolutions - 1:
                if self.resblock_type == "ddpm":
                    h = modules[m_idx](hs[-1])
                    m_idx += 1
                else:
                    h = modules[m_idx](hs[-1], temb)
                    m_idx += 1

                hs.append(h)

        h = hs[-1]
        h = modules[m_idx](h, temb)
        m_idx += 1
        h = modules[m_idx](h, context)  # Spatial Transformer
        m_idx += 1
        h = modules[m_idx](h, temb)
        m_idx += 1

        # Upsampling block
        for i_level in reversed(range(self.num_resolutions)):
            for i_block in range(self.num_res_blocks + 1):
                h = modules[m_idx](torch.cat([h, hs.pop()], dim=1), temb)
                m_idx += 1

                h = modules[m_idx](h, context)  # Spatial Transformer
                m_idx += 1

            if i_level != 0:
                if self.resblock_type == "ddpm":
                    h = modules[m_idx](h)
                    m_idx += 1
                else:
                    h = modules[m_idx](h, temb)
                    m_idx += 1

        assert not hs

        h = self.act(modules[m_idx](h))
        m_idx += 1
        h = modules[m_idx](h)
        m_idx += 1

        assert m_idx == len(modules)
        if self.config.model.scale_by_sigma:
            used_sigmas = used_sigmas.reshape((x.shape[0], *([1] * len(x.shape[1:]))))
            h = h / used_sigmas

        return h
