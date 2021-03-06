import os
import math

import parmap
import heatmap
from tqdm import tqdm
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter

from model.generator import Generator
from model.ACM_discriminator import ACMDiscriminator
from utils.loss import calcul_gp
from utils.layers import weights_init, reset_grads
from utils.image import read_img, resize_img, torch2np
from utils.utils import creat_reals_pyramid, generate_noise, upsampling

global_att_dir = None
global_epoch = None


def save_heatmap(args):
    img = args[0]
    for idx, att in enumerate(args[1]):
        heatmap.add(img, att, alpha=0.4, save=f'{global_att_dir}/{global_epoch}_{args[2]}_{idx}.png', axis='off')


class SinGAN_ACM:
    def __init__(self, config):
        self.config = config
        self.Gs = []
        self.Ds = []
        self.Zs = []
        self.reals = []
        self.noise_amps = []
        self.writer = None
        self.first_img_input = 0
        self.log_losses = {}

    def init_single_layer_gan(self):
        generator = Generator(self.config).to(self.config.device)
        generator.apply(weights_init)
        if self.config.generator_path is not None:
            generator.load_state_dict(torch.load(self.config.generator_path))
        # print(generator)

        discriminator = ACMDiscriminator(self.config, self.config.num_heads, self.reals[len(self.Gs)]).to(self.config.device)
        discriminator.apply(weights_init)
        if self.config.discriminator_path is not None:
            discriminator.load_state_dict(torch.load(self.config.discriminator_path))
        print(discriminator)

        return discriminator, generator

    def train(self):
        # Prepare image pyramid
        train_img = read_img(self.config)
        real = resize_img(train_img, self.config.start_scale, self.config)
        self.reals = creat_reals_pyramid(real, self.reals, self.config)

        prev_nfc = 0
        self.writer = SummaryWriter(f'{self.config.exp_dir}/logs')

        # Pyramid training
        for scale_iter in range(self.config.stop_scale+1):
            # Become larger as scale_iter increase (maximum=128)
            self.config.nfc = min(self.config.nfc_init * pow(2, math.floor(scale_iter / 4)), 128)
            self.config.min_nfc = min(self.config.min_nfc_init * pow(2, math.floor(scale_iter / 4)), 128)

            # Prepare directory to save images
            self.config.result_dir = f'{self.config.exp_dir}/{scale_iter}'
            self.config.att_dir = f'{self.config.result_dir}/attention'
            os.makedirs(self.config.att_dir, exist_ok=True)
            plt.imsave(f'{self.config.result_dir}/real_scale.png', torch2np(self.reals[scale_iter]), vmin=0, vmax=1)

            cur_discriminator, cur_generator = self.init_single_layer_gan()
            if prev_nfc == self.config.nfc:
                cur_generator.load_state_dict(torch.load(f'{self.config.exp_dir}/{scale_iter - 1}/generator.pth'))
                cur_discriminator.load_state_dict(torch.load(f'{self.config.exp_dir}/{scale_iter - 1}/ACM_discriminator.pth'))

            cur_z, cur_generator, cur_discriminator = self.train_single_stage(cur_discriminator, cur_generator)
            cur_generator = reset_grads(cur_generator, False)
            cur_generator.eval()
            cur_discriminator = reset_grads(cur_discriminator, False)
            cur_discriminator.eval()

            self.Gs.append(cur_generator)
            self.Ds.append(cur_discriminator)
            self.Zs.append(cur_z)
            self.noise_amps.append(self.config.noise_amp)

            torch.save(self.Zs, f'{self.config.exp_dir}/Zs.pth')
            torch.save(self.Gs, f'{self.config.exp_dir}/Gs.pth')
            torch.save(self.Ds, f'{self.config.exp_dir}/Ds.pth')
            torch.save(self.reals, f'{self.config.exp_dir}/reals.pth')
            torch.save(self.noise_amps, f'{self.config.exp_dir}/noiseAmp.pth')

            prev_nfc = self.config.nfc
            del cur_discriminator, cur_generator

        return

    def train_single_stage(self, cur_discriminator, cur_generator):
        real = self.reals[len(self.Gs)]
        _, _, real_h, real_w = real.shape

        # Set padding layer(Initial padding) - To Do (Change this for noise padding not zero-padding)g
        self.config.receptive_field = self.config.kernel_size + ((self.config.kernel_size - 1) * (self.config.num_layers - 1)) * self.config.stride
        padding_size = int(((self.config.kernel_size - 1) * self.config.num_layers) / 2)
        noise_pad = nn.ZeroPad2d(int(padding_size))
        image_pad = nn.ZeroPad2d(int(padding_size))

        # MultiStepLR: lr *= gamma every time reaches one of the milestones
        D_optimizer = optim.Adam(cur_discriminator.parameters(), lr=self.config.d_lr, betas=(self.config.beta1, self.config.beta2))
        G_optimizer = optim.Adam(cur_generator.parameters(), lr=self.config.g_lr, betas=(self.config.beta1, self.config.beta2))
        D_scheduler = optim.lr_scheduler.MultiStepLR(optimizer=D_optimizer, milestones=self.config.milestones, gamma=self.config.gamma)
        G_scheduler = optim.lr_scheduler.MultiStepLR(optimizer=G_optimizer, milestones=self.config.milestones, gamma=self.config.gamma)

        # Calculate noise amp(amount of info to generate) and recover prev_rec image
        if not self.Gs:
            rec_z = generate_noise([1, real_h, real_w], device=self.config.device).expand(1, 3, real_h, real_w)
            self.first_img_input = torch.full([1, self.config.img_channel, real_h, real_w], 0, device=self.config.device)
            upscaled_prev_rec_img = self.first_img_input
            self.config.noise_amp = 1
        else:
            rec_z = torch.full([1, self.config.img_channel, real_h, real_w], 0, device=self.config.device)
            upscaled_prev_rec_img = self.draw_sequentially('rec', noise_pad, image_pad)
            criterion = nn.MSELoss()
            rmse = torch.sqrt(criterion(real, upscaled_prev_rec_img))
            self.config.noise_amp = self.config.noise_amp_init * rmse
        padded_rec_z = noise_pad(rec_z)
        padded_rec_img = image_pad(upscaled_prev_rec_img)

        for epoch in tqdm(range(self.config.num_iter), desc=f'{len(self.Gs)}th GAN'):
            random_z = generate_noise([1, real_h, real_w], device=self.config.device).expand(1, 3, real_h, real_w) \
                if not self.Gs else generate_noise([self.config.img_channel, real_h, real_w], device=self.config.device)
            padded_random_z = noise_pad(random_z)

            # Train Discriminator: Maximize D(x) - D(G(z)) -> Minimize D(G(z)) - D(X)
            for i in range(self.config.n_critic):
                # Make random image input
                upscaled_prev_random_img = self.draw_sequentially('rand', noise_pad, image_pad)
                padded_random_img = image_pad(upscaled_prev_random_img)
                padded_random_img_with_z = self.config.noise_amp * padded_random_z + padded_random_img

                # Calculate loss with real data
                cur_discriminator.zero_grad()
                real_prob_out, real_acm_oth, real_add_att_maps, real_sub_att_maps = cur_discriminator(real)
                d_real_loss = -real_prob_out.mean()                         # Maximize D(X) -> Minimize -D(X)

                # Calculate loss with fake data
                fake = cur_generator(padded_random_img_with_z.detach(), padded_random_img)
                fake_prob_out, fake_acm_oth, _, _ = cur_discriminator(fake.detach())
                d_fake_loss = fake_prob_out.mean()                          # Minimize D(G(z))

                # Gradient penalty
                gradient_penalty = calcul_gp(cur_discriminator, real, fake, self.config.device)

                # Update parameters
                d_loss = d_real_loss + d_fake_loss + (gradient_penalty * self.config.gp_weights)
                if self.config.use_acm_oth:
                    d_loss += (torch.abs(real_acm_oth.mean()) + torch.abs(fake_acm_oth.mean())) * self.config.acm_weights
                d_loss.backward()
                D_optimizer.step()

                # Log losses
                critic = -d_real_loss.item() -d_fake_loss.item()            # D(x) - D(G_z)
                self.log_losses[f'{len(self.Gs)}th_D/d'] = d_loss.item()
                self.log_losses[f'{len(self.Gs)}th_D/d_critic'] = critic
                self.log_losses[f'{len(self.Gs)}th_D/d_gp'] = gradient_penalty.item()
                if self.config.use_acm_oth:
                    self.log_losses[f'{len(self.Gs)}th_D/d_oth'] = real_acm_oth.item() + fake_acm_oth.item()

            # Train Generator : Maximize D(G(z)) -> Minimize -D(G(z))
            for i in range(self.config.generator_iter):
                cur_generator.zero_grad()

                # Make fake sample for every iteration
                upscaled_prev_random_img = self.draw_sequentially('rand', noise_pad, image_pad)
                padded_random_img = image_pad(upscaled_prev_random_img)
                padded_random_img_with_z = self.config.noise_amp * padded_random_z + padded_random_img
                fake = cur_generator(padded_random_img_with_z.detach(), padded_random_img)

                # Adversarial loss
                fake_prob_out, _, fake_add_att_maps, fake_sub_att_maps = cur_discriminator(fake)
                g_adv_loss = -fake_prob_out.mean()

                # Reconstruction loss
                mse_criterion = nn.MSELoss()
                padded_rec_img_with_z = self.config.noise_amp * padded_rec_z + padded_rec_img
                g_rec_loss = mse_criterion(cur_generator(padded_rec_img_with_z.detach(), padded_rec_img), real)

                # Update parameters
                g_loss = g_adv_loss + (g_rec_loss * self.config.rec_weights)
                g_loss.backward()
                G_optimizer.step()

                # Log losses
                self.log_losses[f'{len(self.Gs)}th_G/g'] = g_loss
                self.log_losses[f'{len(self.Gs)}th_G/g_critic'] = -g_adv_loss.item()
                self.log_losses[f'{len(self.Gs)}th_G/g_rec'] = g_rec_loss.item()

            # Log losses
            for key, value in self.log_losses.items():
                self.writer.add_scalar(key, value, epoch)
            self.log_losses = {}

            # Log image
            if epoch % self.config.img_save_iter == 0 or epoch == (self.config.num_iter - 1):
                np_real = torch2np(real)
                np_fake = torch2np(fake.detach())
                plt.imsave(f'{self.config.result_dir}/{epoch}_fake_sample.png', np_fake, vmin=0, vmax=1)
                plt.imsave(f'{self.config.result_dir}/{epoch}_fixed_noise.png', torch2np(padded_rec_img_with_z.detach() * 2 - 1), vmin=0, vmax=1)
                plt.imsave(f'{self.config.result_dir}/{epoch}_reconstruction.png', torch2np(cur_generator(padded_rec_img_with_z.detach(), padded_rec_img).detach()), vmin=0, vmax=1)
                real_add_att_maps = real_add_att_maps.detach().to(torch.device('cpu')).numpy().transpose(1, 2, 3, 0)
                real_sub_att_maps = real_sub_att_maps.detach().to(torch.device('cpu')).numpy().transpose(1, 2, 3, 0)
                fake_add_att_maps = fake_add_att_maps.detach().to(torch.device('cpu')).numpy().transpose(1, 2, 3, 0)
                fake_sub_att_maps = fake_sub_att_maps.detach().to(torch.device('cpu')).numpy().transpose(1, 2, 3, 0)

                global global_att_dir
                global global_epoch
                global_att_dir = self.config.att_dir
                global_epoch = epoch

                parmap.map(save_heatmap, [[np_real, real_add_att_maps, 'real_add'], [np_real, real_sub_att_maps, 'real_sub'],
                                          [np_fake, fake_add_att_maps, 'fake_add'], [np_real, fake_sub_att_maps, 'fake_sub']],
                           pm_pbar=False, pm_processes=4)

            D_scheduler.step()
            G_scheduler.step()

        # Save model weights
        torch.save(cur_generator.state_dict(), f'{self.config.result_dir}/generator.pth')
        torch.save(cur_discriminator.state_dict(), f'{self.config.result_dir}/ACM_discriminator.pth')

        return padded_rec_z, cur_generator, cur_discriminator

    def draw_sequentially(self, mode, m_noise, m_image):
        upscaled_prev = self.first_img_input
        if len(self.Gs) > 0:
            if mode == 'rec':
                count = 0
                for G, padded_rec_z, cur_real, next_real, noise_amp in zip(self.Gs, self.Zs, self.reals, self.reals[1:], self.noise_amps):
                    upscaled_prev = upscaled_prev[:, :, 0:cur_real.shape[2], 0:cur_real.shape[3]]
                    padded_img = m_image(upscaled_prev)
                    padded_img_with_z = noise_amp * padded_rec_z + padded_img
                    generated_img = G(padded_img_with_z.detach(), padded_img)
                    up_scaled_img = resize_img(generated_img, 1/self.config.scale_factor, self.config)
                    upscaled_prev = up_scaled_img[:, :, 0:next_real.shape[2], 0:next_real.shape[3]]
                    count += 1
            elif mode == 'rand':
                count = 0
                pad_noise = int(((self.config.kernel_size - 1) * self.config.num_layers) / 2)
                for G, padded_rec_z, cur_real, next_real, noise_amp in zip(self.Gs, self.Zs, self.reals, self.reals[1:], self.noise_amps):
                    if count == 0:  # Generate random 1-channel noise
                        random_noise = generate_noise([1, padded_rec_z.shape[2] - 2 * pad_noise, padded_rec_z.shape[3] - 2 * pad_noise], device=self.config.device)
                        random_noise = random_noise.expand(1, 3, random_noise.shape[2], random_noise.shape[3])
                    else:           # Generate random 3-channel noise
                        random_noise = generate_noise([self.config.img_channel, padded_rec_z.shape[2] - 2 * pad_noise, padded_rec_z.shape[3] - 2 * pad_noise], device=self.config.device)
                    padded_noise = m_noise(random_noise)
                    upscaled_prev = upscaled_prev[:, :, 0:cur_real.shape[2], 0:cur_real.shape[3]]
                    padded_img = m_image(upscaled_prev)
                    padded_img_with_z = noise_amp * padded_noise + padded_img
                    generated_img = G(padded_img_with_z.detach(), padded_img)
                    up_scaled_img = resize_img(generated_img, 1/self.config.scale_factor, self.config)
                    upscaled_prev = up_scaled_img[:, :, 0:next_real.shape[2], 0:next_real.shape[3]]
                    count += 1
        return upscaled_prev

    def load_trained_weights(self):
        if os.path.exists(self.config.exp_dir):
            self.Gs = torch.load(f'{self.config.exp_dir}/Gs.pth')
            self.Ds = torch.load(f'{self.config.exp_dir}/Ds.pth')
            self.Zs = torch.load(f'{self.config.exp_dir}/Zs.pth')
            self.noise_amps = torch.load(f'{self.config.exp_dir}/noiseAmp.pth')
            self.reals = torch.load(f'{self.config.exp_dir}/reals.pth')

    def create_inference_input(self):
        real = self.reals[self.config.gen_start_scale]
        real_scaled = upsampling(real, self.config.scale_h * real.shape[2], self.config.scale_w * real.shape[3])
        if self.config.gen_start_scale == 0:
            start_img_input = torch.full(real_scaled.shape, 0, device=self.config.device)
        else:
            start_img_input = upsampling(real_scaled, real_scaled.shape[2], real_scaled.shape[3])
        return start_img_input

    def create_sr_inference_input(self, real, iter_num):
        resized_real = real
        pad = nn.ZeroPad2d(5)
        finest_G = self.Gs[-1]
        finest_D = self.Ds[-1]
        finest_noise_amp = self.noise_amps[-1]

        self.Zs = []
        self.Gs = []
        self.Ds = []
        self.reals = []
        self.noise_amps = []

        for i in range(iter_num):
            resized_real = resize_img(resized_real, pow(1 / self.config.scale_factor, 1), self.config)
            print(f'{self.config.infer_dir}/{0}_real.png')
            plt.imsave(f'{self.config.infer_dir}/{0}_real.png', torch2np(resized_real), vmin=0, vmax=1)
            self.reals.append(resized_real)
            self.Gs.append(finest_G)
            self.Ds.append(finest_D)
            self.noise_amps.append(finest_noise_amp)
            rec_z = torch.full(resized_real.shape, 0, device=self.config.device)
            padded_rec_z = pad(rec_z)
            self.Zs.append(padded_rec_z)

        return self.reals[0]

    def inference(self, start_img_input):
        if self.config.save_attention_map:
            global global_att_dir
            global global_epoch
            global_att_dir = f'{self.config.infer_dir}/attention'
            os.makedirs(global_att_dir, exist_ok=True)

        if start_img_input is None:
            start_img_input = torch.full(self.reals[0].shape, 0, device=self.config.device)

        cur_images = []

        for idx, (G, D, Z_opt, noise_amp, real) in enumerate(zip(self.Gs, self.Ds, self.Zs, self.noise_amps, self.reals)):
            padding_size = ((self.config.kernel_size - 1) * self.config.num_layers) / 2
            pad = nn.ZeroPad2d(int(padding_size))
            output_h = (Z_opt.shape[2] - padding_size * 2) * self.config.scale_h
            output_w = (Z_opt.shape[3] - padding_size * 2) * self.config.scale_w

            prev_images = cur_images
            cur_images = []

            for i in tqdm(range(self.config.num_samples)):
                if idx == 0:
                    random_z = generate_noise([1, output_h, output_w], device=self.config.device)
                    random_z = random_z.expand(1, 3, random_z.shape[2], random_z.shape[3])
                    padded_random_z = pad(random_z)
                else:
                    random_z = generate_noise([self.config.img_channel, output_h, output_w], device=self.config.device)
                    padded_random_z = pad(random_z)

                if self.config.use_fixed_noise and idx < self.config.gen_start_scale:
                    padded_random_z = Z_opt

                if not prev_images:
                    padded_random_img = pad(start_img_input)
                else:
                    prev_img = prev_images[i]
                    upscaled_prev_random_img = resize_img(prev_img, 1 / self.config.scale_factor, self.config)
                    if self.config.mode == "train_SR":
                        padded_random_img = pad(upscaled_prev_random_img)
                    else:
                        upscaled_prev_random_img = upscaled_prev_random_img[:, :,
                                                   0:round(self.config.scale_h * self.reals[idx].shape[2]),
                                                   0:round(self.config.scale_w * self.reals[idx].shape[3])]
                        padded_random_img = pad(upscaled_prev_random_img)
                        padded_random_img = padded_random_img[:, :, 0:padded_random_z.shape[2], 0:padded_random_z.shape[3]]
                        padded_random_img = upsampling(padded_random_img, padded_random_z.shape[2], padded_random_z.shape[3])

                padded_random_img_with_z = noise_amp * padded_random_z + padded_random_img
                cur_image = G(padded_random_img_with_z.detach(), padded_random_img)

                np_cur_image = torch2np(cur_image.detach())
                if self.config.save_all_pyramid:
                    plt.imsave(f'{self.config.infer_dir}/{i}_{idx}.png', np_cur_image, vmin=0, vmax=1)
                    if self.config.save_attention_map:
                        np_real = torch2np(real)
                        _, _, cur_add_att_maps, cur_sub_att_maps = D(cur_image.detach())
                        cur_add_att_maps = cur_add_att_maps.detach().to(torch.device('cpu')).numpy().transpose(1, 2, 3, 0)
                        cur_sub_att_maps = cur_sub_att_maps.detach().to(torch.device('cpu')).numpy().transpose(1, 2, 3, 0)
                        global_epoch = f'{i}_{idx}thG'
                        parmap.map(save_heatmap, [[np_cur_image, cur_add_att_maps, 'infer_add'], [np_real, cur_sub_att_maps, 'infer_sub']],
                                   pm_pbar=False, pm_processes=2)

                elif idx == len(self.reals) - 1:
                    plt.imsave(f'{self.config.infer_dir}/{i}.png', np_cur_image, vmin=0, vmax=1)
                    if self.config.save_attention_map:
                        np_real = torch2np(real)
                        _, _, cur_add_att_maps, cur_sub_att_maps = D(cur_image.detach())
                        cur_add_att_maps = cur_add_att_maps.detach().to(torch.device('cpu')).numpy().transpose(1, 2, 3, 0)
                        cur_sub_att_maps = cur_sub_att_maps.detach().to(torch.device('cpu')).numpy().transpose(1, 2, 3, 0)
                        global_epoch = f'{i}_{idx}thG'
                        parmap.map(save_heatmap, [[np_cur_image, cur_add_att_maps, 'infer_add'], [np_real, cur_sub_att_maps, 'infer_sub']],
                                   pm_pbar=False, pm_processes=2)

                cur_images.append(cur_image)

        return cur_image.detach()

