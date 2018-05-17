from __future__ import print_function

import datetime
import os
import random
import argparse
from functools import partial
from itertools import islice, product
import pickle

from tqdm import tqdm

from coral import CORAL

os.environ['CUDA_VISIBLE_DEVICES'] = '2'
n_gpus = os.environ['CUDA_VISIBLE_DEVICES'].count(',')+1

import numpy as np
from PIL import Image, ImageDraw

import torch
import torch.optim as optim
import torch.nn.functional as F
import torch.utils.data
import torch.backends.cudnn as cudnn
from torch.autograd import Variable

import torchvision.transforms as transforms
from torchcv.evaluations import voc_eval
from torchcv.models.ssd import SSD300, SSDBoxCoder
from torchcv.loss import SSDLoss
from torchcv.utils.tee_stdout import tee_stdout
from visda import prepare_data

from torch_two_sample import MMDStatistic

parser = argparse.ArgumentParser(description='PyTorch SSD Training')
parser.add_argument('--lr', default=1e-4, type=float, help='learning rate')
parser.add_argument('--resume', '-r', action='store_true', help='resume from checkpoint')
parser.add_argument('--model',
                    default='/scratch2/model_weights/ssd300_12_imagenet.pth',
                    type=str, help='initialized model path')
parser.add_argument('--checkpoint',
                    default='/scratch/run/pyssd/ssd300_12_coral.pth',
                    type=str, help='checkpoint path')
args = parser.parse_args()

# Model
print('==> Building model..')
num_classes = 12
net_single = SSD300(num_classes=num_classes)
net_single.load_state_dict(torch.load(args.model))

best_loss = float('inf')  # best test loss
start_epoch = 0  # start from epoch 0 or last epoch
if args.resume:
    print('==> Resuming from checkpoint..')
    checkpoint = torch.load(args.checkpoint)
    net_single.load_state_dict(checkpoint['net'])
    best_loss = checkpoint['loss']
    start_epoch = checkpoint['epoch']

# Dataset
print('==> Preparing dataset..')
box_coder = SSDBoxCoder(net_single)
batch_size = 8
visda_data = prepare_data(
        box_coder, batch_size=batch_size, n_workers=0,
        img_size=net_single.steps[-1],
        data_paths={
            'vda_root': '/scratch2/mytmp/render_detection_result/png_json',
            'vda_list_train': '/scratch2/mytmp/render_detection_result/listdataset/visda18-detection-train.txt',
            'vda_list_test': '/scratch2/mytmp/render_detection_result/listdataset/visda18-detection-test.txt',
            'coco_root': '/scratch2/data/coco17/train2017',
            'coco_root_test': '/scratch2/data/coco17/val2017',
            'coco_list_train': '/scratch2/mytmp/render_detection_result/listdataset/coco17-train.txt',
            'coco_list_test': '/scratch2/mytmp/render_detection_result/listdataset/coco17-val.txt'
        }
)


cat_of_interest = ['aeroplane', 'bicycle', 'bus', 'car', 'horse', 'knife',
                   'motorcycle', 'person', 'plant', 'skateboard', 'train', 'truck']


class UnNormalize(object):
    def __init__(self, mean, std):
        self.mean = mean
        self.std = std

    def __call__(self, tensor):
        for t, m, s in zip(tensor, self.mean, self.std):
            t.mul_(s).add_(m)
            # The normalize code -> t.sub_(m).div_(s)
        return tensor


unnormalize = UnNormalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))


class ExponentialAverageOf(object):
    def __init__(self, target, source, alpha=0.999):
        self.params = list(target)
        self.src_params = list(source)
        self.alpha = alpha

        for p, src_p in zip(self.params, self.src_params):
            p.data[:] = src_p.data[:]

    def step(self):
        one_minus_alpha = 1.0 - self.alpha
        for p, src_p in zip(self.params, self.src_params):
            p.data.mul_(self.alpha)
            p.data.add_(src_p.data * one_minus_alpha)


def draw_boxes(input_, loc, cls, path, name):
    boxes, labels, scores = box_coder.decode(loc, cls)
    img: Image = transforms.ToPILImage()(unnormalize(input_))
    draw = ImageDraw.Draw(img)
    if boxes is not None:
        for box, label in zip(boxes, labels):
            draw.rectangle(list(box), outline='red')
            draw.text((box[0], box[1]), cat_of_interest[label], fill='red')
    os.makedirs(path, exist_ok=True)
    img.save(os.path.join(path, name+'.png'))


gpu_range = range(n_gpus)
if len(gpu_range) > 1:
    net_extractor = torch.nn.DataParallel(net_single.extractor, device_ids=gpu_range).cuda()
    net = torch.nn.DataParallel(net_single, device_ids=gpu_range).cuda()
else:
    net = net_single.cuda()
    net_extractor = net.extractor
    
cudnn.benchmark = True

criterion = SSDLoss(num_classes=num_classes)
# optimizer = optim.SGD(
#         net.parameters(), lr=args.lr, momentum=0.9, weight_decay=1e-4
# )
optimizer = optim.Adam(
        net.parameters(), lr=1e-4, betas=(0.8, 0.9), weight_decay=1e-4
)

batch_n_max_count = None

distance_name = 'coral'

if distance_name == 'mmd':
    mmd_stat = MMDStatistic(batch_size, batch_size)
    alphas = torch.from_numpy(np.logspace(-2, 2, 10))
    compute_distance = partial(mmd_stat, alphas=alphas)
elif distance_name == 'coral':
    compute_distance = CORAL
else:
    raise ValueError(f'Unknown distance {distance_name}')

output_id = 'output'
mmd_between_ids = [output_id]
mmd_weights = [1]
lambda_ = 0
sample_as_single_feature = False


def flattern(tensor):
    if sample_as_single_feature is True:
        return tensor.view(tensor.size(0), -1)
    else:
        return tensor.view(-1, tensor.size(-1))


def train(epoch):
    print('\nEpoch: %d' % epoch)
    net.train()
    train_loss = 0
    train_slice = islice(
            enumerate(zip(visda_data.source_train_loader,
                          visda_data.target_train_loader)),
            batch_n_max_count
    )
    for batch_idx, joint_data_batch in train_slice:
        (source_inputs, loc_targets, cls_targets), target_inputs = joint_data_batch
        this_batch_size = source_inputs.size(0)
        source_inputs = Variable(source_inputs.cuda())
        loc_targets = Variable(loc_targets.cuda())
        cls_targets = Variable(cls_targets.cuda())
        target_inputs = Variable(target_inputs.cuda())
    
        optimizer.zero_grad()
        loc_preds_s, cls_preds_s = net(source_inputs)  # [batch_size,
        detection_loss = criterion(loc_preds_s, loc_targets, cls_preds_s, cls_targets)
    
        mmds_to_add = []
        other_stats = []
    
        assert len(mmd_weights) == len(mmd_between_ids)
        if output_id in mmd_between_ids:
            weight_index = mmd_between_ids.index(output_id)
            loc_preds_t, cls_preds_t = net(target_inputs)
            mmd_loc = compute_distance(flattern(loc_preds_s), flattern(loc_preds_t))
            mmd_cls = compute_distance(flattern(cls_preds_s), flattern(cls_preds_t))
            if epoch == 0 and batch_idx == 0:
                print(loc_preds_s.size(), '->', flattern(loc_preds_s).size())
                print(cls_preds_s.size(), '->', flattern(cls_preds_s).size())
            mmd_loss = (mmd_loc + mmd_cls)*mmd_weights[weight_index]
            mmds_to_add.append(mmd_loss)
            other_stats.append(f'h[output]: ({mmd_loc.data[0]}+{mmd_cls.data[0]})')
    
        mmd_iterator = [
            (x, w) for x, w in zip(mmd_between_ids, mmd_weights) if x != output_id
        ]
        
        features_all_source = net_extractor(source_inputs)
        features_all_target = net_extractor(target_inputs)
        for mmd_between_id, weight in mmd_iterator:
            assert isinstance(mmd_between_id, int)
            source_features = features_all_source[mmd_between_id]
            target_features = features_all_target[mmd_between_id]
            mmd_loss = compute_distance(
                    flattern(source_features), flattern(target_features)
            )
            if epoch == 0 and batch_idx == 0:
                print(source_features.size(), '->', flattern(source_features).size())
        
            mmds_to_add.append(mmd_loss * weight)
            other_stats.append(f'h[{mmd_between_id}]: {mmd_loss.data[0]}')
    
        full_mmd_loss = sum(mmds_to_add)
        loss = detection_loss + lambda_ * full_mmd_loss
        loss.backward()
        optimizer.step()
        train_loss += loss.data[0]
    
        other_stat = ' '.join(other_stats)
        print(f'{distance_name}_loss: {full_mmd_loss.data[0]} {other_stat} | '
              f'train_loss: {detection_loss.data[0]:.3f} '
              f'full: {loss.data[0]:.3f}'
              f'| avg_loss: {train_loss / (batch_idx + 1):.3f} '
              f'[{batch_idx + 1}/{len(visda_data.source_train_loader)}]')


def test(name, network, epoch_i, dataloader, test_boxes_labels,
         samples_to_draw, image_folder, checkpoint=False):
    print('\nTest')
    network.eval()
    test_loss = 0
    pred_boxes, pred_labels, pred_scores = [], [], []
    gt_boxes, gt_labels = [], []
    for batch_idx, data_point in islice(enumerate(dataloader), batch_n_max_count):
        inputs, loc_targets, cls_targets = data_point
        inputs = Variable(inputs.cuda(), volatile=True)
        loc_targets = Variable(loc_targets.cuda(), volatile=True)
        cls_targets = Variable(cls_targets.cuda(), volatile=True)
        
        print(f'{name}: ', end='')
        loc_preds, cls_preds = network(inputs)
        loss = criterion(loc_preds, loc_targets, cls_preds, cls_targets)
        test_loss += loss.data[0]
        print(f'train_loss: %.3f | avg_loss: %.3f [%d/%d]'
              % (loss.data[0], test_loss / (batch_idx + 1), batch_idx + 1,
                 len(dataloader)))

        for input_i in range(inputs.size(0)):
            if (batch_idx, input_i) in samples_to_draw:
                draw_boxes(inputs[input_i].data.cpu(), loc_preds[input_i].data.cpu(),
                           F.softmax(cls_preds[input_i].float(), dim=1).data.cpu(),
                           image_folder,
                           f'{batch_idx}-{input_i}-{epoch_i}-pred')

            box_preds, label_preds, score_preds = box_coder.decode(
                    loc_preds[input_i].data.cpu(),
                    F.softmax(cls_preds[input_i].float(), dim=1).data.cpu(),
                    score_thresh=0.01, nms_thresh=0.0
            )
            
            loc_unenc, cls_unenc = test_boxes_labels[batch_idx*batch_size+input_i]
        
            pred_boxes.append(box_preds)
            pred_labels.append(label_preds)
            pred_scores.append(score_preds)
            gt_boxes.append(loc_unenc)
            gt_labels.append(cls_unenc)

    voc_loss = voc_eval(pred_boxes, pred_labels, pred_scores, gt_boxes, gt_labels,
                 iou_thresh=0.5, use_07_metric=True)

    avg_loss_result = test_loss / (batch_idx + 1)
    
    print(voc_loss)
    
    return voc_loss, avg_loss_result


k_draw = 5

random.seed(0)
batch_idx_draw_test = list(zip(
        random.choices(range(len(visda_data.testloader)), k=k_draw),
        random.choices(range(visda_data.testloader.batch_size), k=k_draw)
))
batch_idx_draw_coco = list(zip(
        random.choices(range(len(visda_data.cocoloader)), k=k_draw),
        random.choices(range(visda_data.cocoloader.batch_size), k=k_draw)
))


def start_train_test(run_name, run_root, n_epochs):
    global lambda_
    validation_results = []
    for epoch in range(start_epoch, start_epoch + n_epochs):
        lambda_ = (epoch+1)*1e-2
        print('lambda_', lambda_)
        train(epoch)
        test('test', net, epoch,
             visda_data.testloader, visda_data.vda_test_boxes_labels,
             batch_idx_draw_test, os.path.join(run_root, run_name, 'test'))

        validation_results.append(
                test('coco', net, epoch,
                     visda_data.cocoloader, visda_data.coco_test_boxes_labels,
                     batch_idx_draw_coco, os.path.join(run_root, run_name, 'coco'))
        )
        
    return validation_results


def main():
    run_root = '/scratch/run/pyssd/'
    
    results = []
    
    run_name = distance_name
    time_str = datetime.datetime.now().__format__('%d-%b-%y-%H:%M:%S')
    log_fn = os.path.join(
            '/home/grad2/usmn/projects/tfdetect/torchcv/logs',
            run_name+time_str + '.log'
    )

    net_single.load_state_dict(torch.load(args.model))

    with tee_stdout(log_fn):
        validation_results = start_train_test(run_name, run_root, 100)

    results.append((None, validation_results))
    get_map_ = lambda result_tuple: max(r[0]['map'] for r in result_tuple[1])
    sorted_results = sorted(results, key=get_map_, reverse=True)
    print(sorted_results)


main()
