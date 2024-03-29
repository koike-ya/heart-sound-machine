import argparse
import json
from pathlib import Path
from copy import deepcopy

import numpy as np
import pandas as pd
from dataset import ManualDataSet
from librosa.core import load
from ml.models.model_manager import BaseModelManager
from ml.models.pretrained_models import supported_pretrained_models
from ml.src.dataloader import set_dataloader, set_ml_dataloader
from ml.src.metrics import metrics2df, Metric
from ml.src.preprocessor import Preprocessor, preprocess_args
from ml.tasks.train_manager import TrainManager, train_manager_args
from ml.src.gradcam import gradcam_main

DATALOADERS = {'normal': set_dataloader, 'ml': set_ml_dataloader}


def train_args(parser):
    train_manager_args(parser)
    expt_parser = parser.add_argument_group("Experiment arguments")
    expt_parser.add_argument('--expt-id', help='data file for training', default='')
    expt_parser.add_argument('--data-source', help='HSS 1.0 or CinC', default='HSS', choices=['HSS', 'CinC'])
    expt_parser.add_argument('--dataloader-type', help='Dataloader type.', choices=['normal', 'ml'], default='normal')
    expt_parser.add_argument('--gradcam', action='store_true', default=False)

    return parser


def hss_label_func(row):
    return row[1]


def cinc_label_func(row):
    converter = {'Normal': 0, 'Abnormal': 1}
    return converter[row[1]]


def set_load_func(sr, one_audio_sec):
    def load_func(path):
        const_length = sr * one_audio_sec
        wave = load(path[0], sr=sr)[0]
        if wave.shape[0] > const_length:
            wave = wave[:const_length]
        elif wave.shape[0] < const_length:
            n_pad = (const_length - wave.shape[0]) // 2 + 1
            wave = np.pad(wave[:const_length], n_pad)[:const_length]
        return wave.reshape((1, -1))

    return load_func


def create_hss_manifest():
    DATA_DIR = Path(__file__).resolve().parents[1] / 'input'

    db = '1'
    # db = '1.5'
    if db == '1':
        dic = {}
        for phase in ['train', 'devel', 'test']:
            dic[phase] = [str(p.resolve()) for p in (DATA_DIR / 'wav').iterdir() if phase in p.name]
            dic[phase].sort()

        train_dev_label = pd.read_csv(DATA_DIR / 'lab' / 'labels_train_dev.tsv', sep='\t')
        test = pd.read_csv(DATA_DIR / 'lab' / 'labels_test.txt', header=None)

        train = train_dev_label.iloc[:len(dic['train']), :]
        train['file_name'] = dic['train']
        # train = train[train['label'] != 2]
        train.to_csv(DATA_DIR / 'train_manifest.csv', index=False, header=None)

        val = train_dev_label.iloc[len(dic['train']):, :]
        assert val.shape[0] == len(dic['devel'])
        val['file_name'] = dic['devel']
        # val = val[val['label'] != 2]
        val.to_csv(DATA_DIR / 'val_manifest.csv', index=False, header=None)

        test[0] = dic['test']
        test.columns = ['file_name', 'label']
        # test = test[test['label'] != 2]
        test.to_csv(DATA_DIR / 'test_manifest.csv', index=False, header=None)

    elif db == '1.5':
        for phase in ['train', 'devel', 'test']:
            phase_dic = [str(p.resolve()) for p in (DATA_DIR / 'db1-5' / 'wav').iterdir() if phase in p.name]
            phase_dic.sort()

            df = pd.read_csv(DATA_DIR / 'db1-5' / 'lab' / f'labels_{phase}.tsv', sep='\t')
            df['file_name'] = phase_dic
            df.to_csv(DATA_DIR / f'db15_{phase}_manifest.csv', index=False, header=None)


def create_cinc_manifest():
    DATA_DIR = Path(__file__).resolve().parents[1] / 'input'

    head_paths = []
    wav_paths = []
    training_folders = [path.resolve() for path in (DATA_DIR / 'cinc').iterdir() if path.name.startswith('training-')]
    for training_folder in training_folders:
        head_paths.extend([p for p in training_folder.iterdir() if p.name.endswith('.hea')])
        wav_paths.extend([p for p in training_folder.iterdir() if p.name.endswith('.wav')])
    head_paths.sort()
    wav_paths.sort()

    labels = []
    for head, wav in zip(head_paths, wav_paths):
        assert head.name[:-4] == wav.name[:-4]
        with open(head, 'r') as f:
            labels.append(f.read().split('# ')[-1].replace('\n', ''))

    manifest = pd.DataFrame([wav_paths, labels]).T
    manifest.to_csv(DATA_DIR / 'cinc_manifest.csv', header=None, index=False)


def hss_experiment(train_conf) -> float:
    phases = ['train', 'val', 'test']

    if train_conf['task_type'] == 'regress':
        train_conf['class_names'] = [0]
    else:
        train_conf['class_names'] = [0, 1, 2]

    train_conf['prev_classes'] = [0, 1]

    one_audio_sec = 10
    sr = 4000

    dataloaders = {}
    for phase in phases:
        process_func = Preprocessor(train_conf, phase, sr).preprocess
        load_func = set_load_func(sr, one_audio_sec)
        dataset = ManualDataSet(train_conf[f'{phase}_path'], train_conf, load_func, process_func, hss_label_func, phase)
        dataloaders[phase] = DATALOADERS[train_conf['dataloader_type']](dataset, phase, train_conf)

    metrics = [
        Metric('loss', direction='minimize', save_model=True),
        Metric('uar', direction='maximize'),
    ]

    model_manager = BaseModelManager(train_conf['class_names'], train_conf, dataloaders, metrics)

    model_manager.train()
    _, _, metrics = model_manager.test(return_metrics=True)
    del model_manager.model
    uar = [metric for metric in metrics if metric.name == 'uar'][0]

    (Path(__file__).resolve().parent.parent / 'output' / 'params').mkdir(exist_ok=True)
    with open(Path(__file__).resolve().parent.parent / 'output' / 'params' / f"{train_conf['log_id']}.txt", 'w') as f:
        f.write('\nParameters:\n')
        f.write(json.dumps(train_conf, indent=4))

    (Path(__file__).resolve().parent.parent / 'output' / 'metrics').mkdir(exist_ok=True)
    metrics2df(metrics, phase='test').to_csv(
        Path(__file__).resolve().parent.parent / 'output' / 'metrics' / f"{train_conf['log_id']}_test.csv", index=False)

    return uar.average_meter['test'].value


def cv_experiment(train_conf) -> float:
    phases = ['train', 'val', 'test']
    one_audio_sec = 60
    sr = 2000

    if train_conf['task_type'] == 'regress':
        train_conf['class_names'] = [0]
    else:
        train_conf['class_names'] = [0, 1]

    train_conf['prev_classes'] = [0, 1]

    dataset_cls = ManualDataSet
    set_dataloader_func = set_dataloader
    process_func = Preprocessor(train_conf, phase='test', sr=sr).preprocess

    train_val_metrics = [
        Metric('loss', direction='minimize', save_model=True),
        Metric('uar', direction='maximize'),
    ]

    test_metrics = [
        Metric('loss', direction='minimize', save_model=True),
        Metric('uar', direction='maximize'),
        Metric('recall_1', direction='maximize'),
        Metric('specificity', direction='maximize'),
        Metric('f1', direction='maximize'),
    ]
    metrics = {'train': deepcopy(train_val_metrics), 'val': train_val_metrics, 'test': test_metrics}

    load_func = set_load_func(sr, one_audio_sec)
    train_manager = TrainManager(train_conf, load_func, cinc_label_func, dataset_cls, set_dataloader_func, metrics,
                                 process_func=process_func)
    model_manager, val_cv_metrics, test_cv_metrics = train_manager.train_test()

    (Path(__file__).resolve().parent.parent / 'output' / 'params').mkdir(exist_ok=True)
    with open(Path(__file__).resolve().parent.parent / 'output' / 'params' / f"{train_conf['log_id']}.txt", 'w') as f:
        f.write('\nParameters:\n')
        f.write(json.dumps(train_conf, indent=4))

    (Path(__file__).resolve().parent.parent / 'output' / 'metrics').mkdir(exist_ok=True)
    metrics2df(test_cv_metrics, phase='test').to_csv(
        Path(__file__).resolve().parent.parent / 'output' / 'metrics' / f"{train_conf['log_id']}_test.csv", index=False)

    return val_cv_metrics['uar'].mean(), test_cv_metrics


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='train arguments')
    train_conf = vars(train_args(preprocess_args(parser)).parse_args())
    assert train_conf['train_path'] != '' or train_conf['val_path'] != '', \
        'You need to select training, validation data file to training, validation in --train-path, --val-path argments'
    
    test_metric_names = ['uar', 'recall_1', 'specificity', 'f1']

    if train_conf['gradcam']:
        # Gradcam
        assert train_conf['data_source'] == 'CinC', 'now CinC visualization is only available'
        train_conf['class_names'] = [0, 1]
        load_func = set_load_func(sr=2000, one_audio_sec=60)
        process_func = Preprocessor(train_conf, phase='test', sr=2000).preprocess
        dataset = ManualDataSet(train_conf['manifest_path'], train_conf,
                                load_func=load_func, label_func=cinc_label_func, process_func=process_func)
        dataloader = set_dataloader(dataset, 'test', train_conf)
        load_func = set_load_func(sr=2000, one_audio_sec=60)

        gradcam_main(train_conf, dataloader, load_func)
        exit()

    if train_conf['data_source'] == 'HSS':
        create_hss_manifest()
    elif train_conf['data_source'] == 'CinC':
        create_cinc_manifest()

    results = {metric_name: [] for metric_name in test_metric_names}
    val_results = []
    for model in ['vgg16', 'vgg19', 'resnet', 'mobilenet', 'resnext']:
    # for model in ['vgg16', 'vgg19']:
    # for model in ['resnext101', 'resnext101_wsl']:

    # for preprocess in ['spectrogram']:#, 'logmel']:
    # for model in supported_ml_models:
    #     if model not in ('resnet'): continue

        train_conf['model_type'] = model
        print(model)
        #     train_conf['transform'] = preprocess
        #     train_conf['log_id'] = 'mobilenet-' + preprocess

        for lr in [0.0001, 0.00001]:
            train_conf['lr'] = lr
            uar_res = {metric_name: [] for metric_name in test_metric_names}
            val_uar_res = []

            if train_conf['data_source'] == 'HSS':
                for seed in range(5):
                    train_conf['seed'] = seed
                    uar_res.append(hss_experiment(train_conf))
            elif train_conf['data_source'] == 'CinC':
                val_uar, test_metrics = cv_experiment(train_conf)
                val_uar_res.append(val_uar)
                for metric_name in test_metric_names:
                    uar_res[metric_name].append(test_metrics[metric_name].mean())

            # print(np.array(uar_res).mean())
            # print(np.array(uar_res).std())
            val_results.append(np.array(val_uar_res).mean())
            for metric_name in test_metric_names:
                results[metric_name].append(np.array(uar_res[metric_name]).mean())

    expt_path = Path(__file__).resolve().parent.parent / 'output' / f"{train_conf['log_id']}.csv"
    print(val_results)
    print(results)
    # pd.DataFrame(results, index=list(supported_pretrained_models.keys())).T.to_csv(expt_path, index=False)
