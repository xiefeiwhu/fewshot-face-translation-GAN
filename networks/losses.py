from keras import backend as K
from keras.layers import Lambda, dot
import tensorflow as tf


"""
Basic loss functions
"""

def least_square_loss(x, y):
    return K.mean(K.square(x - y))

def ms_ssim_loss(x, y):
    # https://www.tensorflow.org/api_docs/python/tf/image/ssim_multiscale
    return K.mean(Lambda(
        lambda imgs: 1 - tf.image.ssim_multiscale(imgs[0], imgs[1], max_val=1.0)
    )([x, y]))

def cosine_distance(x, y):
    return K.mean(1 - dot([x, y], axes=-1, normalize=True), axis=-1, keepdims=True)
    
def euclidean_distance(x, y):
    return K.mean(K.sqrt(K.sum(K.square(x - y), axis=-1)))

def hybrid_cos_euc_distance(x, y, w1=1, w2=0.1):
    loss_cos = w1 * cosine_distance(x, y)
    loss_euc = w2 * euclidean_distance(x, y)
    return (loss_cos + loss_euc) / (w1 + w2)

def hinge_loss(loss, m=0.3):
    return K.mean(K.maximum(0., loss - m))
    
def relative_hinge_loss(dist_tar, dist_src, m=0.3):
    # This is basically a triplet loss
    return K.mean(K.maximum(0., (dist_tar - dist_src) + m))

"""
FaceTranslationGANTrainModel loss functions
"""

dist_func = hybrid_cos_euc_distance

def adversarial_loss(net_dis, x_gt, x_recon, x_segm, x_emb, y_recon2, w):
    pred_dis_real, pred_emb_real = net_dis([x_gt, x_segm])
    pred_dis_fake, pred_emb_fake = net_dis([x_recon, x_segm])
    pred_dis_fake2, pred_emb_fake2 = net_dis([y_recon2, x_segm])
    loss_dis = least_square_loss(pred_dis_real, K.ones_like(pred_dis_real))
    loss_dis += least_square_loss(pred_dis_fake, K.zeros_like(pred_dis_fake)) / 2
    loss_dis += least_square_loss(pred_dis_fake2, K.zeros_like(pred_dis_fake2)) / 2
    loss_dis += least_square_loss(pred_emb_real, x_emb)
    loss_gen = w * least_square_loss(pred_dis_fake, K.ones_like(pred_dis_fake))
    loss_gen += w * least_square_loss(pred_dis_fake2, K.ones_like(pred_dis_fake2))
    loss_gen += w * least_square_loss(pred_emb_fake, pred_emb_real)
    #loss_gen_adv += w * least_square_loss(pred_emb_fake, x_emb)
    return loss_gen, loss_dis

def reconstruction_loss(x_gt, x_recon, y_recon2, w):
    loss = w * K.mean(K.abs(x_recon - x_gt))
    #loss += w * ms_ssim_loss(x_recon, x_gt)
    loss += w / 100 * K.mean(K.abs(y_recon2 - x_gt))
    return loss

def embeddings_hinge_loss(emb, emb_gt, w, m=0.3, sep_emb=False):
    if sep_emb:
        emb_sep = Lambda(lambda x: tf.split(x, 2, -1))(emb)
        emb_gt_sep = Lambda(lambda x: tf.split(x, 2, -1))(emb_gt)
        for e, e_gt in zip(emb_sep, emb_gt_sep):
            try:
                loss += w * hinge_loss(dist_func(e_gt, e_gt), m=m)
            except:                
                loss = w * hinge_loss(dist_func(e, e_gt), m=m)
    else:
        loss = w * hinge_loss(dist_func(emb, emb_gt), m=m)
    return loss

def relative_embeddings_loss(emb1, emb1_gt, emb2, w, m=0.5, sep_emb=False):
    if sep_emb:
        emb1_sep = Lambda(lambda x: tf.split(x, 2, -1))(emb1)
        emb1_gt_sep = Lambda(lambda x: tf.split(x, 2, -1))(emb1_gt)
        emb2_sep = Lambda(lambda x: tf.split(x, 2, -1))(emb2)
        for e1, e1_gt, e2 in zip(emb1_sep, emb1_gt_sep, emb2_sep):
            try:
                loss += w * relative_hinge_loss(
                    dist_func(e1, e1_gt), 
                    dist_func(e1, e2),
                    m=m)
            except:                
                loss = w * relative_hinge_loss(
                    dist_func(e1, e1_gt), 
                    dist_func(e1, e2),
                    m=m)
    else:
        loss = w * relative_hinge_loss(
            dist_func(emb1, emb1_gt), 
            dist_func(emb1, emb2),
            m=m)
    return loss

def adversarial_loss_paired(net_dis, x_gt, x_gt_rand, x_recon, y_gt, y_recon, w):
    pred_dis_real = net_dis([x_gt, x_gt_rand])
    pred_dis_fake = net_dis([y_gt, x_gt])
    pred_dis_fake2 = net_dis([x_recon, x_gt_rand])
    pred_dis_fake3 = net_dis([y_recon, y_gt])
    loss_dis = least_square_loss(pred_dis_real, K.ones_like(pred_dis_real))
    loss_dis += least_square_loss(pred_dis_fake, K.zeros_like(pred_dis_fake)) / 3
    loss_dis += least_square_loss(pred_dis_fake2, K.zeros_like(pred_dis_fake2)) / 3
    loss_dis += least_square_loss(pred_dis_fake3, K.zeros_like(pred_dis_fake3)) / 3
    loss_gen = w * least_square_loss(pred_dis_fake2, K.ones_like(pred_dis_fake2)) / 2
    loss_gen += w * least_square_loss(pred_dis_fake3, K.ones_like(pred_dis_fake3)) / 2
    return loss_gen, loss_dis

def semantic_consistency_loss(bisenet, x_gt, y_recon, w):
    """semantic consistency loss that encourages the output face preserving glasses and hair

    We already know that conditional image-to-image approach is capable of
    generating faces with gooe structure. However, it is still tricky to 
    preserve glasses and hair (bang) in previous architecture. Thus, we introduce
    an auxiliary parsing network to inject additional perceptual loss.
    """
    def preproc_bisenet(x, mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)):
        """
        Input:
            x: rgb tensor [-1, +1]
        Output:
            rgb tensor normalize for bisenet
        """
        x = (x + 1) / 2 # [-1,+1] to [0,1]
        x_normalized = (x - mean) / std
        x_normalized = tf.image.resize_bilinear(x_normalized, [512,512], align_corners=True)
        return x_normalized
    out_bisenet_fake = bisenet(Lambda(preproc_bisenet)(y_recon))[0]
    out_bisenet_fake_hair = Lambda(lambda x: x[..., 17:18])(out_bisenet_fake)
    out_bisenet_fake_glasses = Lambda(lambda x: x[..., 6:7])(out_bisenet_fake)
    out_bisenet_real = bisenet(Lambda(preproc_bisenet)(x_gt))[0]
    out_bisenet_real_hair = Lambda(lambda x: x[..., 17:18])(out_bisenet_real)
    out_bisenet_real_glasses = Lambda(lambda x: x[..., 6:7])(out_bisenet_real)
    loss = w / 10 * K.mean(K.abs(out_bisenet_fake - out_bisenet_real))
    loss += w * K.mean(K.abs(out_bisenet_fake_hair - out_bisenet_real_hair))
    loss += w * K.mean(K.abs(out_bisenet_fake_glasses - out_bisenet_real_glasses))
    return loss
