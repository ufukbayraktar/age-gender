import os
import yaml
import json
from pathlib import Path
import tensorflow as tf
from tensorflow.core.framework import summary_pb2
import numpy as np
from collections import deque
from datetime import datetime, timedelta

from age_gender.utils.dataloader import DataLoader
from age_gender.utils.config_parser import get_config
from age_gender.nets.inception_resnet_v1 import InceptionResnetV1
from age_gender.nets.resnet_v2_50 import ResNetV2_50
from age_gender.utils.model_saver import ModelSaver
from age_gender.utils.dataset_json_loader import DatasetJsonLoader
from age_gender.nets.learning_rate_manager import LearningRateManager

models = {'inception_resnet_v1': InceptionResnetV1,
          'resnet_v2_50': ResNetV2_50}


class MetricsWriter:
    def __init__(self, file_name):
        self.file_name = file_name
        self.metrics_list = list()

    def dump(self, batch, files, deque):
        current_metrics = {
            'batch': batch,
            'files': byte_to_str(files.tolist()),
            'mae_deque': [float(n) for n in deque['mae']],
            'accuracy_deque': [float(n) for n in deque['gender_acc']],
            'mae': float(np.mean(deque['mae'])),
            'gender_accuracy': float(np.mean(deque['gender_acc'])),
        }
        self.metrics_list.append(current_metrics)
        json.dump(self.metrics_list, Path(self.file_name).open(mode='w'))


class ModelManager:
    def __init__(self, config):
        # parameters
        self._config = config
        self.learning_rate_manager = LearningRateManager(
            self._config['init']['learning_rate'])
        self.model = models[config['init']['model']]()
        self.num_epochs = config['epochs']
        self.train_size = 0
        self.test_size = None
        self.validation_frequency = None
        self.batch_size = config['batch_size']
        self.val_frequency = config['init']['val_frequency']
        self.mode = config['init']['mode']
        self.pretrained_model_folder_or_file = config['init']['pretrained_model_folder_or_file']
        self.experiment_folder = self.get_experiment_folder(self.mode)

        # operations
        self.global_step = self.model.global_step
        self.train_mode = tf.placeholder(tf.bool)
        self.init_op = None
        self.train_op = None
        self.reset_global_step_op = None
        self.train_summary = None
        self.train_init_op = None
        self.test_summary = None
        self.test_init_op = None
        self.images = tf.placeholder(
            tf.float32, shape=[self.batch_size, 256, 256, 3])
        self.age_labels = tf.placeholder(tf.int32)
        self.gender_labels = tf.placeholder(tf.int32)
        self.train_metrics_deque = {}
        self.test_metrics_deque = {}
        self.train_metrics_writer = MetricsWriter(
            str(Path(self.experiment_folder).joinpath('train_metrics.json')))
        self.test_metrics_writer = MetricsWriter(
            str(Path(self.experiment_folder).joinpath('test_metrics.json')))
        # todo: вынести константы

    def create_metric_deques(self):
        for name in self.test_metrics_and_errors.keys():
            self.test_metrics_deque[name] = deque(maxlen=self.val_frequency)
        for name in self.train_metrics_and_errors.keys():
            self.train_metrics_deque[name] = deque(maxlen=self.val_frequency)

    def train(self):
        os.makedirs(self.experiment_folder, exist_ok=True)
        log_dir = os.path.join(self.experiment_folder, 'logs')
        self.create_computational_graph()
        self.create_metric_deques()
        next_data_element, self.train_init_op, self.train_size = self.init_data_loader(
            'train')
        next_test_data, self.test_init_op, self.test_size = self.init_data_loader(
            'test')

        num_batches = (self.train_size + 1) // self.batch_size
        print(f'Train size: {self.train_size}, test size: {self.test_size}')
        print(
            f'Epochs in train: {self.num_epochs}, batches in epoch: {num_batches}')
        print(f'Validation frequency {self.val_frequency}')
        # print('train_metrics_names:', self.train_metrics_names)

        with tf.Graph().as_default() and tf.Session() as sess:
            tf.random.set_random_seed(100)
            sess.run(self.init_op)
            summary_writer = tf.summary.FileWriter(log_dir, sess.graph)
            sess.run(tf.global_variables_initializer())
            saver = ModelSaver(
                var_list=self.variables_to_restore, max_to_keep=100)
            saver.restore_model(sess, self.pretrained_model_folder_or_file)
            trained_steps = sess.run(self.global_step)
            print('trained_steps', trained_steps)
            trained_epochs = self.calculate_trained_epochs(
                trained_steps, num_batches)
            print('trained_epochs', trained_epochs)

            start_time = {'train': datetime.now()}
            saver.save_hyperparameters(self.experiment_folder, time_spent(
                start_time['train']), self._config)
            fpaths = list()
            if self.mode == 'start':
                sess.run(self.reset_global_step_op)
                trained_steps = 0
                print('global_step turned to zero')
            for tr_batch_idx in range((1+trained_epochs)*num_batches, (1+trained_epochs+self.num_epochs)*num_batches):
                sess.run(self.train_init_op)
                # start_time.update({'train_epoch': datetime.now()})
                train_images, train_age_labels, train_gender_labels, file_paths = sess.run(
                    next_data_element)
                fpaths += [fp.decode('utf-8') for fp in file_paths]
                feed_dict = {self.train_mode: True,
                             # np.zeros([16, 256, 256, 3])
                             self.images: train_images,
                             self.age_labels: train_age_labels,
                             self.gender_labels: train_gender_labels,
                             }
                _, train_metrics_and_errors, step, bottleneck = sess.run([self.train_op, self.train_metrics_and_errors,
                                                                          self.global_step, self.bottleneck],
                                                                         feed_dict=feed_dict)
                #print('step: ', step)
                # print(self.train_metrics_deque)
                self.train_metrics_deque, summaries = get_streaming_metrics(self.train_metrics_deque,
                                                                            train_metrics_and_errors, 'train')
                summary_writer.add_summary(summaries, step)
                self.train_metrics_writer.dump(
                    int(step), file_paths, self.train_metrics_deque)

                if (step - trained_steps) % self.val_frequency == 0:
                    start_time.update({'test_epoch': datetime.now()})
                    sess.run([self.test_init_op])
                    for ts_batch_idx in range(1, self.val_frequency+1):
                        test_images, test_age_labels, test_gender_labels, test_file_paths = sess.run(
                            next_test_data)
                        feed_dict = {
                            self.train_mode: False,
                            self.images: test_images,
                            self.age_labels: test_age_labels,
                            self.gender_labels: test_gender_labels
                        }
                        # summary = sess.run(self.test_summary, feed_dict=feed_dict)
                        # train_writer.add_summary(summary, step - num_batches + batch_idx)
                        test_metrics_and_errors = sess.run(
                            self.test_metrics_and_errors, feed_dict=feed_dict)
                        self.test_metrics_deque, summaries = get_streaming_metrics(self.test_metrics_deque,
                                                                                   test_metrics_and_errors, 'test')
                        current_batch_num = int(
                            step) - self.val_frequency + ts_batch_idx
                        summary_writer.add_summary(
                            summaries, current_batch_num)
                        self.test_metrics_writer.dump(
                            current_batch_num, test_file_paths, self.test_metrics_deque)
                    t = time_spent(start_time['test_epoch'])
                    print(f'Test takes {t}')
                    t = time_spent(start_time['train'])
                    print(
                        f'Train {tr_batch_idx} batches plus test time take {t}')
                    save_path = saver.save(sess, os.path.join(
                        self.experiment_folder, "model.ckpt"), global_step=tr_batch_idx)
                    self.save_hyperparameters(start_time)
                    print("Model saved in file: %s" % save_path)

            saver.save_model(sess, tr_batch_idx, self.experiment_folder)

    def get_experiment_folder(self, mode):
        if mode == 'start':
            working_dir = self._config['working_dir']
            experiment_folder = os.path.join(
                working_dir, 'experiments', datetime.now().strftime("%Y_%m_%d_%H_%M"))
            os.makedirs(experiment_folder, exist_ok=True)
        elif mode == 'continue':
            experiment_folder = \
                self.pretrained_model_folder_or_file if os.path.isdir(self.pretrained_model_folder_or_file) else \
                os.path.dirname(self.pretrained_model_folder_or_file)
        else:
            experiment_folder = 'experiments'
        return experiment_folder

    def calculate_trained_epochs(self, trained_steps,  num_batches):
        # return (trained_steps) // num_batches
        return (trained_steps - self.model.trained_steps) // num_batches

    def save_hyperparameters(self, start_time):
        self._config['duration'] = time_spent(start_time['train'])
        self._config['date'] = datetime.now().strftime("%Y_%m_%d_%H_%M")
        json_parameters_path = os.path.join(
            self.experiment_folder, "hyperparams.yaml")
        with open(json_parameters_path, 'w') as file:
            yaml.dump(self._config, file, default_flow_style=False)

    def create_computational_graph(self):
        self.variables_to_restore, age_logits, gender_logits = self.model.inference(
            self.images)
        # head
        age_label_encoded = tf.one_hot(indices=self.age_labels, depth=101)
        age_cross_entropy = tf.nn.softmax_cross_entropy_with_logits(labels=age_label_encoded,
                                                                    logits=age_logits)
        age_cross_entropy_mean = tf.reduce_mean(age_cross_entropy)
        gender_labels_encoded = tf.one_hot(indices=self.gender_labels, depth=2)
        gender_cross_entropy = tf.nn.softmax_cross_entropy_with_logits(labels=gender_labels_encoded,
                                                                       logits=gender_logits)
        gender_cross_entropy_mean = tf.reduce_mean(gender_cross_entropy)

        # l2 regularization
        age_ = tf.cast(tf.constant([i for i in range(0, 101)]), tf.float32)
        age = tf.reduce_sum(tf.multiply(
            tf.nn.softmax(age_logits), age_), axis=1)
        mae = tf.losses.absolute_difference(self.age_labels, age)
        mse = tf.losses.mean_squared_error(self.age_labels, age)
        gender_acc = tf.reduce_mean(tf.cast(tf.nn.in_top_k(
            gender_logits, self.gender_labels, 1), tf.float32))

        total_loss = tf.add_n(
            [gender_cross_entropy_mean, age_cross_entropy_mean] + tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))

        self.reset_global_step_op = tf.assign(self.global_step, 0)

        lr = self.learning_rate_manager.get_learning_rate(self.global_step)

        metrics_and_errors = {
            'mae': mae,
            'mse': mse,
            'age_cross_entropy_mean': age_cross_entropy_mean,
            'gender_acc': gender_acc,
            'gender_cross_entropy_mean': gender_cross_entropy_mean,
            'total_loss': total_loss
        }
        self.metrics_and_errors = metrics_and_errors
        self.test_metrics_and_errors = self.metrics_and_errors
        self.train_metrics_and_errors = self.metrics_and_errors.copy()
        self.train_metrics_and_errors.update({'lr': lr})

        optimizer = tf.train.AdamOptimizer(lr)
        # update batch normalization layer
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            self.train_op = optimizer.minimize(total_loss, self.global_step)

        self.init_op = tf.group(
            tf.global_variables_initializer(), tf.local_variables_initializer())

        self.bottleneck = [v for v in
                           tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=self.model.bottleneck_scope)]

    def init_data_loader(self, mode):
        dataset_path = self._config['init'][f'{mode}_dataset_path']
        dataset_json_config = self._config['init']['dataset_json_loader']
        dataset_json = json.load(Path(dataset_path).open())
        if self._config['init']['balance_dataset']:
            dataset_json_loader = DatasetJsonLoader(
                dataset_json_config, dataset_json)
            dataset_json = dataset_json_loader.get_dataset()
        data_folder = os.path.dirname(dataset_path)
        loader = DataLoader(dataset_json, data_folder)
        dataset = loader.create_dataset(
            perform_shuffle=True, batch_size=self.batch_size)

        iterator = tf.data.Iterator.from_structure(
            dataset.output_types, dataset.output_shapes)
        next_data_element = iterator.get_next()
        training_init_op = iterator.make_initializer(dataset)
        return next_data_element, training_init_op, loader.dataset_len()


def time_spent(start):
    sec = int((datetime.now() - start).total_seconds())
    return str(timedelta(seconds=sec))


def get_streaming_metrics(metrics_deque, metrics_and_errors, mode):
    summaries_list = list()
    for name in metrics_deque.keys():
        metric = metrics_and_errors[name]
        if name != 'lr':
            metrics_deque[name].append(metric)
            metric = np.mean(metrics_deque[name])
        summary = summary_pb2.Summary.Value(
            tag=f'{mode}/{name}', simple_value=metric)
        summaries_list.append(summary)
    # todo: для train добавить вычисление гистограммы
    summaries = summary_pb2.Summary(value=summaries_list)
    return metrics_deque, summaries


if __name__ == '__main__':
    config = get_config('config.yaml', 'train')
    if not config['cuda']:
        os.environ['CUDA_VISIBLE_DEVICES'] = ''
    ModelManager(config).train()
