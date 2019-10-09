import cv2
import numpy as np
from pathlib import PurePath
from keras.layers import Input, Lambda, Concatenate
from keras import backend as K
from keras.models import Model
import tensorflow as tf
from tensorflow.contrib.distributions import Beta

import networks.generator as gen
import networks.discriminator as dis

class FaceTranslationGANBaseModel:
    def __init__(self, config):
        self.config = config
        self.input_size = int(config["input_size"])
        self.identity_extractor = config["identity_extractor"].lower()
        self.nc_in = int(config["nc_in"])

    def load_weights(self, dir_weights="weights"):     
        raise NotImplementedError()

    def define_inference_path(self):
        image_size = (self.input_size, self.input_size, self.nc_in)
        inp_src = Input(shape=image_size)
        inp_tar = Input(shape=image_size)
        inp_segm = Input(shape=image_size)
        try:
            inp_emb = Input((self.latent_dim,))
            self.path_inference = K.function(
                [inp_src, inp_tar, inp_segm, inp_emb], 
                [self.decoder(self.encoder([inp_src, inp_tar, inp_segm]) + [inp_emb])])
        except:
            raise Exception("Error building inference Keras function.")
        
    def build_encoder(self):
        return gen.encoder(self.nc_in, self.input_size)
        
    def build_decoder(self):
        return gen.decoder(
            512, self.input_size//16, self.nc_in, 
            self.num_fc, self.latent_dim, self.adain_sep_mean_var) 

    def build_discriminator_sem(self):
        """Build the discriminator 1 (semantic consistency).

        This discrimimnator takes RGB images and parsing masks as input.
        """
        return dis.discriminator_conditional(self.nc_in, self.input_size, self.latent_dim)

    def build_discriminator_pa(self):
        """Build discriminator 2 (perceptual awareness).

        This discrimimnator takes RGB image pairs as input.
        """
        return dis.discriminator_perceptually_aware(self.nc_in, self.input_size, self.vggface_feats)

class FaceTranslationGANInferenceModel(FaceTranslationGANBaseModel):
    def __init__(self, config):
        super().__init__(config=config)
        #self.input_size = INPUT_SIZE
        #self.identity_extractor = identity_extractor.lower()
        #self.nc_in = NC_IN

        if self.identity_extractor == "inceptionresnetv1":
            self.latent_dim = int(config["latent_dim"])
            self.num_fc = 3
            self.adain_sep_mean_var = False
        elif self.identity_extractor == "ir50_hybrid":
            self.latent_dim = int(config["latent_dim"]) * 2
            self.num_fc = 5
            self.adain_sep_mean_var = True
        else:
            raise ValueError(f"Received an unknown identity extractor: {identity_extractor}")
        
        try:
            self.encoder = self.build_encoder()
            self.decoder = self.build_decoder()        
        except:
            raise Exception("Error building networks.")
        self.dir_weights = config["dir_weights"]
        self.load_weights(self.dir_weights)   

        self.define_inference_path()     
        
    def load_weights(self, dir_weights):
        try:
            self.encoder.load_weights(str(PurePath(dir_weights, "encoder.h5")))
            self.decoder.load_weights(str(PurePath(dir_weights, "decoder.h5")))
            print(f"Found checkpoints in {dir_weights} folder. Built model with pre-trained weights.")
        except:
            print("No pre-trained weights were found. Model built with default initializaiton.")
            pass
    
    def preprocess_input(self, im):
        im = cv2.resize(im, (self.input_size, self.input_size))
        return im / 255 * 2 - 1
    
    def inference(self, src, mask, tar, emb_tar):
        return self.path_inference(            
            [
                self.preprocess_input(src)[None, ...], 
                self.preprocess_input(tar)[None, ...], 
                self.preprocess_input(mask.astype(np.uint8))[None, ...],
                emb_tar
            ])

class FaceTranslationGANTrainModel(FaceTranslationGANBaseModel):
    def __init__(self, config):
        super().__init__(config=config)

        if self.identity_extractor == "inceptionresnetv1":
            raise NotImplementedError()
        elif self.identity_extractor == "ir50_hybrid":
            self.latent_dim = int(config["latent_dim"]) * 2
            self.num_fc = 5
            self.adain_sep_mean_var = True
        else:
            raise ValueError(f"Received an unknown identity extractor: {identity_extractor}")
        
        # Build generator
        print("Building generator...")
        self.encoder = self.build_encoder()
        self.decoder = self.build_decoder()
        self.dir_weights = config["dir_weights"]
        self.load_weights(self.dir_weights)
        for layer in (self.encoder.layers + self.decoder.layers):
            if "SPADE_norm" in layer.name:
                layer.traibable = False

        # Build auxiliary networks
        print("Building auxiliary networks...")
        self.build_vggface_res50()
        self.build_bisenet()
        self.build_embeddings_extractor()

        # Build discriminators
        print("Building discriminators...")
        self.discriminator_sem = self.build_discriminator_sem()
        self.discriminator_pa = self.build_discriminator_pa()

        print("Building loss functions...")
        self.define_inference_path()
        self.define_variables()
        self.define_losses()
        print("Done")

    def load_weights(self, dir_weights):
        try:
            self.encoder.load_weights(str(PurePath(dir_weights, "encoder.h5")))
            self.decoder.load_weights(str(PurePath(dir_weights, "decoder.h5")))
            self.discriminator_sem.load_weights(str(PurePath(dir_weights, "discriminator_sem.h5")))
            self.discriminator_pa.load_weights(str(PurePath(dir_weights, "discriminator_pa.h5")))
            print(f"Found checkpoints in {dir_weights} folder. Built model with pre-trained weights.")
        except:
            print("No pre-trained weights were found. Model built with default initializaiton.")
            pass

    def save_weights(self, dir_weights, iter=""):
        self.encoder.save_weights(str(PurePath(dir_weights, f"encoder_iter{str(iter)}.h5")))
        self.decoder.save_weights(str(PurePath(dir_weights, f"decoder_iter{str(iter)}.h5")))
        self.discriminator_sem.save_weights(str(PurePath(dir_weights, f"discriminator_sem_iter{str(iter)}.h5")))
        self.discriminator_pa.save_weights(str(PurePath(dir_weights, f"discriminator_pa_iter{str(iter)}.h5")))

    def forward_path(self, x_src, x_ref, x_segm, y_emb):
        return self.decoder(
            self.encoder(
                [x_src, x_ref, x_segm]
            ) + [y_emb]
        )

    def define_variables(self):
        """Define network variables

        Descriptions:
            x: source identity
            y: target identity
        """
        self.rgb_gt_tensor = Input(shape=(self.input_size, self.input_size, 3))
        self.rgb_gt_rand_tensor = Input(shape=(self.input_size, self.input_size, 3))
        self.rgb_inp_tensor = Input(shape=(self.input_size, self.input_size, 3))
        self.segm_tensor = Input(shape=(self.input_size, self.input_size, 3))
        self.hair_mask_tensor = Input(shape=(self.input_size, self.input_size, 3))
        self.rgb_tar_tensor = Input(shape=(self.input_size, self.input_size, 3))

        self.emb_src_rand = self.net_extractor(self.rgb_gt_rand_tensor)
        self.emb_src_gt = self.net_extractor(self.rgb_gt_tensor)
        self.emb_src_inp = self.net_extractor(self.rgb_inp_tensor)
        #self.emb_src_rand_asia = Lambda(lambda x: x[..., :latent_dim//2])(self.emb_src_rand)
        #self.emb_src_rand_ms1m = Lambda(lambda x: x[..., latent_dim//2:])(self.emb_src_rand)
        #self.emb_src_gt_asia = Lambda(lambda x: x[..., :latent_dim//2])(self.emb_src_gt)
        #self.emb_src_gt_ms1m = Lambda(lambda x: x[..., latent_dim//2:])(self.emb_src_gt)
        #self.emb_src_inp_asia = Lambda(lambda x: x[..., :latent_dim//2])(self.emb_src_inp)
        #self.emb_src_inp_ms1m = Lambda(lambda x: x[..., latent_dim//2:])(self.emb_src_inp)

        self.rgb_tar_tensor = Input(shape=(self.input_size, self.input_size, 3))
        self.emb_tar = self.net_extractor(self.rgb_tar_tensor)
        #self.emb_tar_asia = Lambda(lambda x: x[..., :latent_dim//2])(emb_tar)
        #self.emb_tar_ms1m = Lambda(lambda x: x[..., latent_dim//2:])(emb_tar)

        dist_emb = Beta(0.3, 0.3)
        if self.identity_extractor == "inceptionresnetv1":
            lam_emb = dist_emb.sample()
            self.emb_src_mixed = lam_emb_asia * self.emb_src_gt + (1 - lam_emb) * self.emb_src_rand
        elif self.identity_extractor == "ir50_hybrid":
            lam_emb_asia = dist_emb.sample()
            lam_emb_ms1m = dist_emb.sample()
            emb_src_gt_asia, emb_src_gt_ms1m = Lambda(lambda x: tf.split(x, 2, -1))(self.emb_src_gt)
            emb_src_rand_asia, emb_src_rand_ms1m = Lambda(lambda x: tf.split(x, 2, -1))(self.emb_src_rand)
            emb_src_mixed_asia = lam_emb_asia * emb_src_gt_asia + (1 - lam_emb_asia) * emb_src_rand_asia
            emb_src_mixed_ms1m = lam_emb_ms1m * emb_src_gt_ms1m + (1 - lam_emb_ms1m) * emb_src_rand_ms1m
            emb_src_mixed_asia = K.l2_normalize(emb_src_mixed_asia)
            emb_src_mixed_ms1m = K.l2_normalize(emb_src_mixed_ms1m)
            self.emb_src_mixed = Concatenate()([emb_src_mixed_asia, emb_src_mixed_ms1m])

        # x_blurred -> x_recon
        self.rgb_recon_tensor = self.forward_path(
            self.rgb_inp_tensor, 
            self.rgb_gt_rand_tensor, 
            self.segm_tensor,
            self.emb_src_mixed) #
        self.emb_recon_tensor = self.net_extractor(self.rgb_recon_tensor)
        #self.emb_recon_tensor_asia = Lambda(lambda x: x[..., :latent_dim//2])(self.emb_recon_tensor)
        #self.emb_recon_tensor_ms1m = Lambda(lambda x: x[..., latent_dim//2:])(self.emb_recon_tensor)

        # x_blurred -> y_recon
        self.rgb_recon_tar_tensor = self.forward_path(
            self.rgb_inp_tensor, #
            self.rgb_tar_tensor, 
            self.segm_tensor,
            self.emb_tar)

        # x_gt -> y_recon
        self.rgb_recon_tar_tensor2 = self.forward_path(
            self.rgb_gt_tensor, #
            self.rgb_tar_tensor, 
            self.segm_tensor,
            self.emb_tar)

        # x_blurred -> y_recon -> x_recon_recon
        self.emb_recon_tar_tensor = self.net_extractor(self.rgb_recon_tar_tensor)
        self.emb_recon_tar_tensor2 = self.net_extractor(self.rgb_recon_tar_tensor2)
        #self.emb_recon_tar_tensor_asia = Lambda(lambda x: x[..., :latent_dim//2])(self.emb_recon_tar_tensor)
        #self.emb_recon_tar_tensor_ms1m = Lambda(lambda x: x[..., latent_dim//2:])(self.emb_recon_tar_tensor)
        self.rgb_cyclic_tensor =  self.forward_path(
            self.rgb_recon_tar_tensor, 
            self.rgb_gt_rand_tensor, 
            self.segm_tensor,
            self.emb_src_gt)
        self.rgb_cyclic_tensor2 =  self.forward_path(
            self.rgb_recon_tar_tensor2, 
            self.rgb_gt_rand_tensor, 
            self.segm_tensor,
            self.emb_src_gt)

    def define_losses(self):
        from networks.losses import adversarial_loss, reconstruction_loss, embeddings_hinge_loss
        from networks.losses import relative_embeddings_loss, semantic_consistency_loss, adversarial_loss_paired

        w_adv = self.config["loss"]["w_adv"]
        w_adv2 = self.config["loss"]["w_adv2"]
        w_recon = self.config["loss"]["w_recon"]
        w_hc = self.config["loss"]["w_hc"]
        w_cyc = self.config["loss"]["w_cyc"]
        w_emb = self.config["loss"]["w_emb1"]
        w_emb2 = self.config["loss"]["w_emb2"]
        #w_pl = self.config["loss"]["w_pl"]
        w_sl = self.config["loss"]["w_sl"]

        sep_emb = True if self.identity_extractor == "ir50_hybrid" else False

        # Adversarial loss
        self.loss_gen_adv, self.loss_dis = adversarial_loss(
            self.discriminator_sem,
            self.rgb_gt_tensor,
            self.rgb_recon_tensor,
            self.segm_tensor,
            self.emb_src_gt,
            self.rgb_recon_tar_tensor2,
            w_adv
        )
        self.loss_gen = self.loss_gen_adv 

        # Reconstruction regularization
        self.loss_gen_rec = reconstruction_loss(
            self.rgb_gt_tensor, 
            self.rgb_recon_tensor,
            self.rgb_recon_tar_tensor2,
            w_recon
            )
        self.loss_gen += self.loss_gen_rec

        # Latent embedding loss
        self.loss_gen_emb = embeddings_hinge_loss(
            self.emb_recon_tensor, 
            self.emb_src_rand, 
            w_adv,
            sep_emb=sep_emb)
        #self.loss_gen_emb = embeddings_hinge_loss(
        #    self.emb_recon_tensor_asia, 
        #    self.emb_src_rand_asia, 
        #    w_adv)
        #self.loss_gen_emb += embeddings_hinge_loss(
        #    self.emb_recon_tensor_ms1m, 
        #    self.emb_src_rand_ms1m, 
        #    w_adv)
        self.loss_gen += self.loss_gen_emb

        # Perceptual loss
        # Pass

        # Laten embedding loss 2
        # recon_tar should be close to tar face and far from src face
        self.loss_gen_emb2 = embeddings_hinge_loss(
            self.emb_recon_tar_tensor2, 
            self.emb_tar, 
            w_adv,
            m=0.25,
            sep_emb=sep_emb)
        self.loss_gen_emb_relative = relative_embeddings_loss(
            self.emb_recon_tar_tensor2, 
            self.emb_tar, 
            self.emb_src_gt,
            w_emb2,
            m=0.5,
            sep_emb=sep_emb)
        #self.loss_gen_emb2 = embeddings_hinge_loss(
        #    self.emb_recon_tar_tensor2_asia, 
        #    self.emb_tar_asia, 
        #    w_emb2, 
        #    m=0.25) 
        #self.loss_gen_emb2 += embeddings_hinge_loss(
        #    self.emb_recon_tar_tensor2_ms1m, 
        #    self.emb_tar_ms1m, 
        #    w_emb2, 
        #    m=0.25) 
        #self.loss_gen_emb_relative = relative_embeddings_loss(
        #    self.emb_recon_tar_tensor2_asia, 
        #    self.emb_tar_asia, 
        #    self.emb_src_gt_asia,
        #    w_emb2,
        #    m=0.5)
        #self.loss_gen_emb_relative += relative_embeddings_loss(
        #    self.emb_recon_tar_tensor2_ms1m, 
        #    self.emb_tar_ms1m, 
        #    self.emb_src_gt_ms1m,
        #    w_emb2,
        #    m=0.5)
        self.loss_gen2 = self.loss_gen_emb2
        self.loss_gen2 += self.loss_gen_emb_relative

        # Cyclic loss
        self.loss_cyc = w_cyc * K.mean(K.abs(self.rgb_cyclic_tensor - self.rgb_gt_tensor))
        self.loss_cyc = w_cyc * K.mean(K.abs(self.rgb_cyclic_tensor2 - self.rgb_gt_tensor))
        self.loss_gen2 += self.loss_cyc

        # Adversarial loss paired
        self.loss_gen2_paired, self.loss_dis_pair = adversarial_loss_paired(
            self.discriminator_pa,
            self.rgb_gt_tensor,
            self.rgb_gt_rand_tensor,
            self.rgb_recon_tensor,
            self.rgb_tar_tensor,
            self.rgb_recon_tar_tensor2,
            w_adv2
        )
        self.loss_gen2 += self.loss_gen2_paired

        # hair consistency loss
        # L1 loss on masked hair region, require additional hair parsing map
        # This loss is to prevent our model trying to modify attribute outside of face region (i.e., hair should not be added/removed).
        self.loss_hair_consistency = w_hc * K.mean(self.hair_mask_tensor * K.abs(self.rgb_recon_tar_tensor2 - self.rgb_gt_tensor))
        self.loss_hair_consistency += w_hc * K.mean(self.hair_mask_tensor * K.abs(self.rgb_recon_tensor - self.rgb_gt_tensor))
        self.loss_gen2 += self.loss_hair_consistency

        # semantic consistency loss
        self.loss_gen_sl = semantic_consistency_loss(
            self.bisenet, 
            self.rgb_gt_tensor, 
            self.rgb_recon_tar_tensor2, 
            w_sl)
        self.loss_gen2 += self.loss_gen_sl

    def build_embeddings_extractor(self):
        if self.identity_extractor == "inceptionresnetv1":
            raise NotImplementedError() 
        elif self.identity_extractor == "ir50_hybrid":
            self.build_hybrid_ir50s()

    def build_vggface_res50(self):        
        def preprocess_vggface():
            input_tensor = Input((None, None, 3)) # 64x64,[-1, +1] , RGB
            x = Lambda(lambda tensor: tf.image.resize_images(tensor, [226, 226]))(input_tensor)
            output_tensor = Lambda(lambda tensor: (tensor + 1)/2 * 255 - [131.0912, 103.8827, 91.4953])(x)
            return Model(input_tensor, output_tensor)

        from networks.vggface_resnet50 import RESNET50
        vggface = RESNET50(include_top=False, weights=None, input_shape=(226, 226, 3))
        try:
            vggface.load_weights(str(PurePath("weights", "rcmalli_vggface_tf_notop_resnet50.h5")))
        except:
            raise Exception("Error loading pre-trained RESNET50.")

        vggface_feats = Model(
            vggface.inputs, 
            [
                vggface.layers[36].output,
                vggface.layers[78].output,
                vggface.layers[140].output,
                vggface.layers[172].output,
            ])
        self.vggface_feats = Model(
            vggface_feats.inputs, 
            vggface_feats(preprocess_vggface()(vggface_feats.inputs))
            )
        for layer in self.vggface_feats.layers:
            layer.trainable = False

    def build_bisenet(self):
        from face_toolbox_keras.models.parser.BiSeNet.bisenet import BiSeNet_keras
        self.bisenet = BiSeNet_keras()
        try:
            self.bisenet.load_weights(str(PurePath(
                "face_toolbox_keras", 
                "models",
                "parser",
                "BiSeNet",
                "BiSeNet_keras.h5")))
        except:
            raise Exception("Error loading pre-trained BiSeNet.")
        for layer in self.bisenet.layers:
            layer.trainable = False

    def build_hybrid_ir50s(self):
        def preprocess_ir50():
            def preproc(x):
                x = (x + 1) / 2 * 255
                x = (x - 127.5) / 128
                x = x[:, 8:120, 8:120, :]
                return x  
            input_tensor = Input((None, None, 3)) 
            output_tensor = Lambda(preproc)(input_tensor)
            return Model(input_tensor, output_tensor)
        def l2_norm():            
            input_tensor = Input((512,))
            output_tensor = Lambda(lambda x: K.l2_normalize(x))(input_tensor)
            return Model(input_tensor, output_tensor)
        def resize_tensor(size=128):
            input_tensor = Input((None, None, 3)) 
            output_tensor = Lambda(lambda x: tf.image.resize_bilinear(x, [size, size]))(input_tensor)
            return Model(input_tensor, output_tensor)
        def build_hybrid_identity_extractor(weights_path=None):
            from face_toolbox_keras.models.verifier.face_evoLVe_ir50.ir50 import IR50
            input_tensor = Input((None, None, 3)) 
            ir50_asia = IR50(
                weights_path=str(PurePath(
                    "face_toolbox_keras",
                    "models",
                    "verifier",
                    "face_evoLVe_ir50",
                    "backbone_ir50_asia_keras.h5")), 
                model_name="IR50_asia")
            ir50_ms1m = IR50(
                weights_path=str(PurePath(
                    "face_toolbox_keras",
                    "models",
                    "verifier",
                    "face_evoLVe_ir50",
                    "backbone_ir50_ms1m_keras.h5")), 
                model_name="IR50_ms1m")
            preprocess_layer = preprocess_ir50()
            resize_layer = resize_tensor(size=128)
            l2_normalize = l2_norm()
            output_ir50_asia = l2_normalize(ir50_asia(preprocess_layer(resize_layer(input_tensor))))
            output_ir50_ms1m = l2_normalize(ir50_ms1m(preprocess_layer(resize_layer(input_tensor))))
            output_tensor = Concatenate()([output_ir50_asia, output_ir50_ms1m])
            return Model(input_tensor, output_tensor)

        self.net_extractor = build_hybrid_identity_extractor()
        for layer in self.net_extractor.layers:
            layer.trainable = False

    def get_generator_trainable_weights(self):
        weights_gen = self.encoder.trainable_weights
        weights_gen += self.decoder.trainable_weights
        return weights_gen

    def get_discriminator_trainable_weights(self):
        weights_dis = self.discriminator_sem.trainable_weights
        weights_dis += self.discriminator_pa.trainable_weights
        return weights_dis
    
    def get_generator_total_loss(self):
        return self.loss_gen + self.loss_gen2

    def get_discriminator_total_loss(self):
        return self.loss_dis + self.loss_dis_pair

    def get_generator_update_tensors(self):
        update_inp = [
            self.rgb_gt_tensor, 
            self.segm_tensor, 
            self.rgb_inp_tensor, 
            self.rgb_gt_rand_tensor, 
            self.rgb_tar_tensor, 
            self.hair_mask_tensor]
        update_out = [
            self.loss_gen, 
            self.loss_gen_adv, 
            self.loss_gen_rec, 
            self.loss_gen_emb, 
            self.loss_gen_emb2, 
            self.loss_gen_emb_relative, 
            self.loss_gen_adv2, 
            self.loss_cyc, 
            self.loss_gen_sl]
        return update_inp, update_out

    def get_discriminator_update_tensors(self):
        update_inp = [
            self.rgb_gt_tensor, 
            self.segm_tensor, 
            self.rgb_inp_tensor, 
            self.rgb_gt_rand_tensor, 
            self.rgb_tar_tensor, 
            self.hair_mask_tensor]
        update_out = [
            self.loss_dis, 
            self.loss_dis_pair]
        return update_inp, update_out
