import tensorflow as tf
import tensorflow.contrib.slim as slim
import tensorflow.contrib.slim.nets as nets
import numpy as np
import time
import os
import cv2

from dataset import TFRecordDataset
from PIL import Image


tf.logging.set_verbosity(tf.logging.INFO)


FLAGS = tf.flags.FLAGS
tf.flags.DEFINE_integer('num_steps', '50000', 'number of steps for optimization')
tf.flags.DEFINE_integer('batch_size', '2', 'batch size for training')
tf.flags.DEFINE_integer('num_classes', '3', 'number of classes in dataset')
tf.flags.DEFINE_float('learning_rate', '2e-4', 'learning rate for optimizer')
tf.flags.DEFINE_float('momentum', '0.99', 'momentum for Momentum Optimizer')
tf.flags.DEFINE_float('lr_decay_rate', '0.99', 'decay rate of learning rate')
tf.flags.DEFINE_bool('lr_decay', 'True', 'exponentially decay learning rate')
tf.flags.DEFINE_string('ckpt_path', 'vgg_16_160830.ckpt', 'path to checkpoint')
tf.flags.DEFINE_string('log_dir', 'ckpt_180918_v1', 'path to logging directory')
tf.flags.DEFINE_string('data_dir', 'data', 'path to dataset')
tf.flags.DEFINE_string('data_name', 'Cityscapes', 'name of dataset')
tf.flags.DEFINE_string('mode', 'train', 'either train or valid')
tf.flags.DEFINE_string('optimizer', 'Adam', 'supports momentum and Adam')


def FCN8_atonce(images, num_classes):
    
    paddings = tf.constant([[0, 0], [96, 96], [96, 96], [0, 0]])
    pad_images = tf.pad(images, paddings, 'CONSTANT')

    model = nets.vgg
    with slim.arg_scope(model.vgg_arg_scope()):
        score, end_points = model.vgg_16(pad_images, num_classes, spatial_squeeze=False)
    
    with tf.variable_scope('FCN'):
        score_pool3 = slim.conv2d(0.0001 * end_points['vgg_16/pool3'], num_classes, 1, scope='score_pool3')
        score_pool4 = slim.conv2d(0.01 * end_points['vgg_16/pool4'], num_classes, 1, scope='score_pool4')
    
        score_pool3c = tf.image.central_crop(score_pool3, 7 / 13)
        score_pool4c = tf.image.central_crop(score_pool4, 7 / 13)

        up_score = slim.conv2d_transpose(score, num_classes, 4, stride=2, scope='up_score')
        fuse1 = tf.add(up_score, score_pool4c, name='fuse1')

        up_fuse1 = slim.conv2d_transpose(fuse1, num_classes, 4, stride=2, scope='up_fuse1')
        fuse2 = tf.add(up_fuse1, score_pool3c, name='fuse2')

        up_fuse2 = slim.conv2d_transpose(fuse2, num_classes, 16, stride=8, scope='up_fuse2')

        pred = tf.argmax(up_fuse2, 3, name='pred')

    return tf.expand_dims(pred, 3), up_fuse2


def IOU_for_label(gt, pred, label):
    
    gt_bin = np.copy(gt)
    gt_bin[gt_bin != label] = 0

    pred_bin = np.copy(pred)
    pred_bin[pred_bin != label] = 0
                
    I = np.logical_and(gt_bin, pred_bin)
    U = np.logical_or(gt_bin, pred_bin)
    return np.count_nonzero(I) / np.count_nonzero(U)


def main(_):
    
    log_dir = FLAGS.log_dir

    '''
     Setting up the model
    '''
    dataset = TFRecordDataset(FLAGS.data_dir, FLAGS.data_name)
    images, gts, org_images, num_samples = dataset.load_batch(FLAGS.mode, FLAGS.batch_size if FLAGS.mode == 'train' else 1)

    pred, logits = FCN8_atonce(images, FLAGS.num_classes)

    if FLAGS.mode == 'valid':

        saver = tf.train.Saver(slim.get_variables_to_restore())
        coord = tf.train.Coordinator()
        
        with tf.Session() as sess:
            
            '''
             Restore parameters from check point
            '''
            saver.restore(sess, tf.train.latest_checkpoint(log_dir))

            tf.train.start_queue_runners(sess, coord)
            
            eval_dir = os.path.join(log_dir, 'eval')
            if not tf.gfile.Exists(eval_dir):
                tf.gfile.MakeDirs(eval_dir)

            IOU = 0
            exp = int(np.log10(num_samples)) + 1
            
            time_per_image = time.time()
            for i in range(num_samples):

                r_images, r_gts, r_pred = sess.run([org_images, gts, pred])
                
                r_images = np.squeeze(r_images)
                r_gts = np.squeeze(r_gts)
                r_gts = r_gts.astype(np.uint8)
                r_pred = np.squeeze(r_pred)
                r_pred = r_pred.astype(np.uint8)

                IOU += IOU_for_label(r_gts, r_pred, 2)
                
                res = r_images.shape;
                output = np.zeros((res[0], 3 * res[1], 3), dtype=np.uint8)

                r_gts = cv2.applyColorMap(r_gts * 80, cv2.COLORMAP_JET)
                r_pred = cv2.applyColorMap(r_pred * 80, cv2.COLORMAP_JET)

                r_images = 0.8 * r_images + 0.2 * r_pred

                output[:, 0*res[1]:1*res[1], :] = r_images
                output[:, 1*res[1]:2*res[1], :] = r_gts
                output[:, 2*res[1]:3*res[1], :] = r_pred

                cv2.imwrite(os.path.join(eval_dir, FLAGS.mode + str(i).zfill(exp) + '.png'), output)
                
            coord.request_stop()
            coord.join()
            
            time_per_image = (time.time() - time_per_image) / num_samples
            print('time elapsed: ' + str(time_per_image))

            IOU /= num_samples
            print('IOU for foreground: ' + str(IOU))


    elif FLAGS.mode == 'train':
        '''
         Define the loss function
        '''
        loss = tf.losses.sparse_softmax_cross_entropy(logits=logits, labels=tf.squeeze(gts))
        total_loss = tf.losses.get_total_loss()

        '''
         Define summaries
        '''
        tf.summary.image('image', images)
        tf.summary.image('gt', tf.cast(gts * 80, tf.uint8))
        tf.summary.image('pred', tf.cast(pred * 80, tf.uint8))
        tf.summary.scalar('loss', loss)

        '''
         Define initialize function
        '''
        exclude = ['vgg_16/fc8', 'FCN']
        variables_to_restore = slim.get_variables_to_restore(exclude=exclude)

        init_fn = tf.contrib.framework.assign_from_checkpoint_fn(FLAGS.ckpt_path, variables_to_restore, ignore_missing_vars = True)

        '''
         Define the learning rate
        '''
        if (FLAGS.lr_decay):
            num_epochs_before_decay = 2

            num_batches_per_epoch = num_samples / FLAGS.batch_size
            num_steps_per_epoch = num_batches_per_epoch  # Because one step is one batch processed
            decay_steps = int(num_epochs_before_decay * num_steps_per_epoch)

            lr = tf.train.exponential_decay(
                    learning_rate = FLAGS.learning_rate,
                    global_step = tf.train.get_or_create_global_step(),
                    decay_steps = decay_steps,
                    decay_rate = FLAGS.lr_decay_rate,
                    staircase = True)
        else:
            lr = FLAGS.learning_rate

        '''
         Define the optimizer
        '''
        if (FLAGS.optimizer == 'momentum'):
            optimizer = tf.train.MomentumOptimizer(learning_rate=lr, momentum=FLAGS.momentum)
        elif (FLAGS.optimizer == 'Adam'):
            optimizer = tf.train.AdamOptimizer(learning_rate=lr)
        else:
            print('Unknown name of optimizer')
    
        '''
         Training phase
        '''
        if not tf.gfile.Exists(log_dir):
            tf.gfile.MakeDirs(log_dir)

        # generate a log to save hyper-parameter info
        with open(os.path.join(log_dir, 'info.txt'), 'w') as f:
            f.write('num_steps: ' + str(FLAGS.num_steps) + '\n')
            f.write('batch_size: ' + str(FLAGS.batch_size) + '\n')
            f.write('learning_rate: ' + str(FLAGS.learning_rate) + '\n')
            f.write('momentum: ' + str(FLAGS.momentum) + '\n')
            f.write('lr_decay_rate: ' + str(FLAGS.lr_decay_rate) + '\n')
            f.write('lr_decay: ' + str(FLAGS.lr_decay) + '\n')
            f.write('ckpt_path: ' + FLAGS.ckpt_path + '\n')
            f.write('data_dir: ' + FLAGS.data_dir + '\n')
            f.write('data_name: ' + FLAGS.data_name + '\n')
            f.write('mode: ' + FLAGS.mode + '\n')
            f.write('optimizer: ' + FLAGS.optimizer)

        train_op = slim.learning.create_train_op(total_loss, optimizer)

        final_loss = slim.learning.train(
                train_op = train_op,
                logdir = log_dir,
                init_fn = init_fn,
                number_of_steps = FLAGS.num_steps,
                summary_op = tf.summary.merge_all(),
                save_summaries_secs = 120,
                save_interval_secs = 240)

        print('Finished training. Final batch loss %f' %final_loss)


    else:

        print('Unknown mode')


if __name__ == "__main__":
    tf.app.run()