from datetime import datetime

from models.base import buildAccuracy
import matplotlib.pyplot as plt
import tensorflow as tf
import numpy as np
from scipy.spatial import distance
from sklearn.metrics import confusion_matrix
import itertools
import tensorflow.contrib.slim as slim
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import math_ops
from tensorflow.contrib import layers

flags = tf.app.flags
FLAGS = flags.FLAGS
_FRAMES_PER_SECOND = 12


class Trainer(object):

    def __init__(self, model_1, model_2, logger=None, display_freq=1,
                 learning_rate=0.0001, num_classes=128, num_epochs=1, nr_frames=12, temporal_pooling=False):

        self.model_transfer = model_1
        self.model_2 = model_2
        self.logger = logger

        self.display_freq = display_freq
        self.learning_rate = learning_rate
        self.num_classes = num_classes
        self.num_epochs = num_epochs
        self.nr_frames = nr_frames
        self.temporal_pooling = temporal_pooling
        self.acoustic = False
        # # Extract input model shape
        self.shape_2 = [self.model_2.height, self.model_2.width, self.model_2.channels]
        self.transfer_shape = [self.model_transfer.height, self.model_transfer.width,
                               self.model_transfer.channels]
        self.alpha = FLAGS.alpha

    def _build_functions(self, data):
        self.handle = tf.placeholder(tf.string, shape=())
        self.epoch = tf.placeholder(tf.int32, shape=())
        iterator = tf.data.Iterator.from_string_handle(self.handle, data.data.output_types,
                                                       data.data.output_shapes)
        iterat = data.data.make_initializable_iterator()
        next_batch = iterator.get_next()
        # give directly batch tensor depending on the network reshape
        audio_data, acoustic_data, labels, scenario = self._retrieve_batch(next_batch)
        self.labels = labels
        # generate random couples
        # positive_outputANDnegative_output = self.mix_data(acoustic_data)
        # labels are a placeholder but we compute an array with shuffle_data then given as input
        # shuffle pairs
        # input_video, input_acoustic = self.shuffle_data( anchor_output, positive_output, negative_output)
        # build model with tensor data next batch
        with tf.device('/gpu:0'):
            self.model_2._build_model(audio_data)
            self.model_transfer._build_model(acoustic_data)  # positive_outputANDnegative_output
        # find logits after defining next batch and iterator

        # temporal pooling gives one predition for nr_frames, if it is not we have one predicition for frame

        # normalize vector of audio with positive and then negative
        logits_2_reshape = self.model_2.output
        temperature_value = 1
        logits_transfer = self.model_transfer.output  # network[7]
        logits_transfer = tf.nn.softmax(logits_transfer / temperature_value)
        expanded_shape = [-1, FLAGS.sample_length * _FRAMES_PER_SECOND, self.num_classes]
        transferweighted = tf.reduce_mean(tf.reshape(logits_transfer, shape=expanded_shape), axis=1)

        self.cross_loss = tf.losses.softmax_cross_entropy(
            onehot_labels=self.labels,
            logits=logits_2_reshape,
            weights=1.0 - self.alpha,
            scope='cross_loss'
        )
        self.dist_loss = tf.losses.softmax_cross_entropy(
            onehot_labels=transferweighted,
            logits=logits_2_reshape,
            weights=self.alpha,
            scope='dist_loss'
        )
        self.loss = tf.losses.get_total_loss()

        # Define accuracy
        self.accuracy = buildAccuracy(logits_2_reshape, self.labels)

        # Initialize counters and stats
        self.global_step = tf.train.create_global_step()

        # Define optimizer
        # before different
        self.optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        self.train_vars = self.model_2.train_vars
        with tf.control_dependencies(update_ops):
            with tf.device('/gpu:0'):
                # Compute the gradients for acoustic variables.
                # Compute the gradients for visual variables.
                grads_and_vars = self.optimizer.compute_gradients(self.loss, self.model_2.train_vars)
                # Ask the optimizer to apply the gradients.
                self.train_op_1 = self.optimizer.apply_gradients(grads_and_vars, global_step=self.global_step)

        # Initialize model saver
        self.saver = tf.train.Saver(max_to_keep=None)
        return iterat

    def _get_optimizer_variables(self, optimizer):

        optimizer_vars = [optimizer.get_slot(var, name)
                          for name in optimizer.get_slot_names() for var in self.train_vars if var is not None]

        optimizer_vars.extend(list(optimizer._get_beta_accumulators()))

        return optimizer_vars

    def _init_models(self, session):

        # if there is restore checkpoint restore all not single model
        if FLAGS.restore_checkpoint is not None:
            # Restore session from checkpoint
            self._restore_model(session)
        # initialize two stream
        elif FLAGS.visual_init_checkpoint is not None or FLAGS.acoustic_init_checkpoint is not None:
            # not to have uninitialized value if only one model is initialized
            session.run(tf.global_variables_initializer())
            # Initialize global step
            print('{}: {} - Initializing global step'.format(datetime.now(), FLAGS.exp_name))
            session.run(self.global_step.initializer)
            print('{}: {} - Done'.format(datetime.now(), FLAGS.exp_name))

            # don't initialize with sgd and momentum
            # Initialize optimizer variables
            print('{}: {} - Initializing optimizer variables'.format(datetime.now(), FLAGS.exp_name))
            optimizer_vars = self._get_optimizer_variables(self.optimizer)
            optimizer_init_op = tf.variables_initializer(optimizer_vars)
            session.run(optimizer_init_op)
            print('{}: {} - Done'.format(datetime.now(), FLAGS.exp_name))
            if FLAGS.acoustic_init_checkpoint is not None:
                # Initialize acoustic model
                print('{}: {} - Initializing model'.format(datetime.now(), FLAGS.exp_name))
                self.model_2.init_model(session, FLAGS.acoustic_init_checkpoint)
            print('{}: {} - Done'.format(datetime.now(), FLAGS.exp_name))
        else:
            # Initialize all variables
            print('{}: {} - Initializing full model'.format(datetime.now(), FLAGS.exp_name))
            session.run(tf.global_variables_initializer())
            print('{}: {} - Done'.format(datetime.now(), FLAGS.exp_name))

    def _restore_model(self, session):

        print('{}: {} - Restoring session'.format(datetime.now(), FLAGS.exp_name))
        # Restore model
        session.run(tf.global_variables_initializer())
        # FLAGS.model == 'DualCamHybridNet':
        to_exclude = [i.name for i in tf.global_variables()
                      if
                      'beta' in i.name or 'hear_net' in i.name or 'resnet_v1' in i.name or 'global_step' in i.name]  # or 'resnet_v1' in i.name
        var_list = slim.get_variables_to_restore(exclude=to_exclude)

        # else:
        #     var_list = slim.get_model_variables(self.model.scope)
        saver = tf.train.Saver(var_list=var_list)
        saver.restore(session, FLAGS.restore_checkpoint)
        print('{}: {} - Done'.format(datetime.now(), FLAGS.exp_name))

    def train(self, train_data=None, valid_data=None):

        # Assert training and validation sets are not None
        assert train_data is not None
        assert valid_data is not None

        # compute loss and minimize
        train_iterat = self._build_functions(train_data)
        eval_iterat = valid_data.data.make_initializable_iterator()
        # Add the variables we train to the summary
        # for var in self.model.train_vars:
        #     self.logger.log_histogram(var.name, var)

        # # Disable image logging
        # self.logger.log_image('input', self.model.network['input'])
        # self.logger.log_sound('input', self.model.network['input'])
        # # Log attention map
        self.logger.log_scalar('cross_entropy_loss', self.cross_loss)
        self.logger.log_scalar('distillation_loss', self.dist_loss)
        self.logger.log_scalar('train_loss', self.loss)

        # Add the accuracy to the summary
        self.logger.log_scalar('train_accuracy', self.accuracy)

        # Merge all summaries together
        self.logger.merge_summary()

        # Start training session
        with tf.Session(config=tf.ConfigProto(allow_soft_placement=True, log_device_placement=True,
                                              gpu_options=tf.GPUOptions(
                                                  allow_growth=True), )) as session:  # allow_growth=False to occupy all space in GPU
            train_handle = session.run(train_iterat.string_handle())
            evaluation_handle = session.run(eval_iterat.string_handle())
            # Initialize model either randomly or with a checkpoint
            self._init_models(session)

            # Add the model graph to TensorBoard
            self.logger.write_graph(session.graph)
            # Save model
            self._save_checkpoint(session, 'random')
            start_epoch = int(tf.train.global_step(session, self.global_step) / train_data.total_batches)
            best_epoch = -1
            best_accuracy = -1.0
            best_loss = 10000.0

            # For each epoch
            for epoch in range(start_epoch, start_epoch + self.num_epochs):
                # Initialize counters and stats
                step = 0

                # Initialize iterator over the training set
                # session.run(training_init_op)  , feed_dict={train_data.seed: epoch})
                session.run(train_iterat.initializer)
                # For each mini-batch
                while True:
                    try:
                        cross_loss, dist_loss, train_loss, train_summary, _ = session.run(
                            # train_accuracy,
                            [self.cross_loss, self.dist_loss, self.loss, self.logger.summary_op,
                             self.train_op_1],  #
                            feed_dict={self.handle: train_handle,  # self.accuracy,
                                       self.epoch: epoch,
                                       self.model_2.network['is_training']: 1,
                                       self.model_2.network['keep_prob']: 0.5})

                        # Compute mini-batch error
                        if step % self.display_freq == 0:
                            print(
                                '{}: {} - Iteration: [{:3}]\t Training_Loss: {:6f}\t cross_loss: {:6f}\t dist_loss: {:6f}'.format(
                                    # \t Training_Accuracy: {:6f}
                                    datetime.now(), FLAGS.exp_name, step, train_loss, cross_loss,
                                    dist_loss))  # , train_accuracy
                            self.logger.write_summary(train_summary, tf.train.global_step(session, self.global_step))
                            self.logger.flush_writer()
                        # Update counters and stats
                        step += 1

                    except tf.errors.OutOfRangeError:
                        break

                # Evaluate model on validation set
                session.run(eval_iterat.initializer)
                total_loss, total_accuracy = self._evaluate(session, 'validation', evaluation_handle)  #

                print('{}: {} - Epoch: {}\t loss: {:6f}\t Validation_Accuracy: {:6f}'.format(datetime.now(),
                                                                                                     FLAGS.exp_name,
                                                                                                     epoch,
                                                                                                     total_loss,
                                                                                                     total_accuracy))

                self.logger.write_summary(tf.Summary(value=[
                    tf.Summary.Value(tag="valid_loss", simple_value=total_loss),
                    tf.Summary.Value(tag="valid_accuracy", simple_value=total_accuracy)
                ]), epoch)

                self.logger.flush_writer()
                # if multiple of 10 epochs save model
                # if epoch % 1 == 0:
                #     best_epoch = epoch
                #     best_accuracy = total_accuracy
                #     best_loss = cross_loss
                #     # Save model
                #     self._save_checkpoint(session, epoch)
                #     with open('{}/{}'.format(FLAGS.checkpoint_dir, FLAGS.exp_name) + "/model_{}.txt".format(epoch),
                #               "w") as outfile:
                #         outfile.write(
                #             '{}: {} - Epoch: {}\t Validation_Loss: {:6f}\t Validation_Accuracy: {:6f}'.format(
                #                 datetime.now(),
                #                 FLAGS.exp_name,
                #                 best_epoch,
                #                 best_loss, best_accuracy))

                # if accuracy or loss decrease save model
                if total_accuracy >= best_accuracy:
                    best_epoch = epoch
                    best_accuracy = total_accuracy
                    best_loss = total_loss
                    # Save model
                    name = 'best'
                    self._save_checkpoint(session, name)
                    with open('{}/{}'.format(FLAGS.checkpoint_dir, FLAGS.exp_name) + "/model_{}.txt".format(name),
                              "w") as outfile:
                        outfile.write(
                            '{}: {} - Best Epoch: {}\t Validation_Loss: {:6f}\t Validation_Accuracy: {:6f}'.format(  #
                                datetime.now(),
                                FLAGS.exp_name,
                                best_epoch,
                                best_loss, best_accuracy))  #
            print('{}: {} - Best Epoch: {}\t cross_loss: {:6f}\t Validation_Accuracy: {:6f}'.format(datetime.now(),  #
                                                                                                      FLAGS.exp_name,
                                                                                                      best_epoch,
                                                                                                      best_loss,
                                                                                                      best_accuracy))  #

    def _save_checkpoint(self, session, epoch):

        checkpoint_dir = '{}/{}'.format(FLAGS.checkpoint_dir, FLAGS.exp_name)
        model_name = 'model_{}.ckpt'.format(epoch)
        print('{}: {} - Saving model to {}/{}'.format(datetime.now(), FLAGS.exp_name, checkpoint_dir, model_name))

        self.saver.save(session, '{}/{}'.format(checkpoint_dir, model_name))

    def _valid(self, session, evaluation_handle):
        return self._evaluate(session, 'validation', evaluation_handle)

    def _evaluate(self, session, mod, eval_handle):
        # Initialize counters and stats
        loss_sum = 0.0
        accuracy_sum = 0.0
        data_set_size = 0  # 0.0
        label = []
        pred = []
        # For each mini-batch
        while True:
            try:
                # Compute batch loss and accuracy
                # compute accuracy with corresponding vectors
                batch_loss, batch_accuracy, labels_data = session.run(  # labels_data, batch_pred, batch_accuracy,
                    [self.loss, self.accuracy, self.labels],
                    # self.labels, self.batch_pred, self.accuracy,
                    feed_dict={self.handle: eval_handle,
                               self.epoch: 0,
                               self.model_2.network['is_training']: 0,
                               self.model_2.network['keep_prob']: 1.0})



                # Update counters
                data_set_size += np.shape(labels_data)[0]  # 1 labels_data.shape[0]
                loss_sum += batch_loss * np.shape(labels_data)[0]  # labels_data.shape[0]
                accuracy_sum += batch_accuracy * np.shape(labels_data)[0]
            except tf.errors.OutOfRangeError:
                break
        # print (data_set_size)
        print(loss_sum)
        # print (accuracy_sum)
        total_loss = loss_sum / float(data_set_size)
        total_accuracy = accuracy_sum / float(data_set_size)
        # if mod == 'test':
        #     self.plot_confusion_matrix(pred, label)
        return total_loss, total_accuracy

    def _retrieve_batch(self, next_batch):

        acoustic_tr_data = tf.reshape(next_batch[0],
                                      shape=[-1, self.transfer_shape[0], self.transfer_shape[1],
                                             self.transfer_shape[2]])  # , dtype=tf.float32
        acoustic_data = tf.reshape(next_batch[1],
                                   shape=[-1, self.shape_2[0], self.shape_2[1], self.shape_2[2]])
        labels = tf.reshape(next_batch[3], shape=[-1, 10])
        scenario = tf.reshape(next_batch[4], shape=[-1, 61])
        return acoustic_data, acoustic_tr_data, labels, scenario

    def test(self, test_data=None):

        # Assert testing set is not None
        assert test_data is not None
        eval_iterat = self._build_functions(test_data)
        # Create a one-shot iterator
        # iterator = test_data.data.make_one_shot_iterator()
        # next_batch = iterator.get_next()
        # Start training session
        with tf.Session(config=tf.ConfigProto(gpu_options=tf.GPUOptions(allow_growth=True))) as session:  # allow_growth
            evaluation_handle = session.run(eval_iterat.string_handle())
            # Initialize model either randomly or with a checkpoint if given
            self._restore_model(session)
            session.run(eval_iterat.initializer)
            # Evaluate model over the testing set
            test_loss, test_accuracy = self._evaluate(session, 'test', evaluation_handle)

        print('{}: {} - Testing_Loss: {:6f}\t Testing_Accuracy: {:6f}'.format(datetime.now(),
                                                                              FLAGS.exp_name,
                                                                              test_loss, test_accuracy))

        return test_loss, test_accuracy

    def plot_confusion_matrix(self, pred, label, normalize=True,
                              title='Confusion matrix'):
        """
        This function prints and plots the confusion matrix.
        Normalization can be applied by setting `normalize=True`.
        """
        counter = 0
        cmap = plt.cm.Blues
        cm = confusion_matrix(label, pred)
        percentage2 = label.shape[0]
        for i in range(percentage2):
            if (pred[i] == label[i]):
                counter += 1

        perc = counter / float(percentage2)
        print(perc)
        classes = ['False', 'True']
        if normalize:
            cm = cm.astype('float') / cm.sum(axis=1)[:, np.newaxis]
            print("Normalized confusion matrix")
        else:
            print('Confusion matrix, without normalization')

        print(cm)
        # cmap = plt.cm.get_cmap('Blues')
        plt.imshow(cm, interpolation='nearest', cmap=cmap)
        plt.title(title)
        plt.colorbar()
        tick_marks = np.arange(len(classes))
        plt.xticks(tick_marks, classes, rotation=90)
        plt.yticks(tick_marks, classes)

        fmt = '.2f' if normalize else 'd'
        thresh = cm.max() / 2.
        for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
            plt.text(j, i, format(cm[i, j], fmt),
                     horizontalalignment="center",
                     color="white" if cm[i, j] > thresh else "black")

        plt.ylabel('True label')
        plt.xlabel('Predicted label')
        plt.tight_layout()
        plt.show()
        # plt.savefig('/data/vsanguineti/confusion_matrix_hearnet_transfer.png')