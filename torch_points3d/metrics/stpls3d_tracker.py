from typing import Dict, Any
import logging
import torch
from torch_geometric.nn.unpool import knn_interpolate
import numpy as np
import os
import os.path as osp
import _pickle as pickle
import matplotlib.pyplot as plt
from sklearn.metrics import confusion_matrix
from sklearn.utils.multiclass import unique_labels
from torch_points3d.metrics.confusion_matrix import ConfusionMatrix
from torch_points3d.metrics.segmentation_tracker import SegmentationTracker
from torch_points3d.metrics.base_tracker import BaseTracker, meter_value
from torch_points3d.datasets.change_detection import IGNORE_LABEL
from torch_points3d.core.data_transform import SaveOriginalPosId
from torch_points3d.models import model_interface

log = logging.getLogger(__name__)

STPLS3D_NB_CLASS = 15
INV_OBJECT_LABEL = {
    0: "Ground",
    1: "Build",
    2: "LowVeg",
    3: "MediumVeg",
    4: "HiVeg",
    5: "Vehicle",
    6: "Truck",
    7: "Aircraft",
    8: "MilitaryVec",
    9: "Bike",
    10: "Motorcycle",
    11: "LightPole",
    12: "StreetSign",
    13: "Clutter",
    14: "Fence"
}

class STPLS3DTracker(SegmentationTracker):
    def __init__(self, dataset, stage="train", wandb_log=False, use_tensorboard: bool = False,
                 ignore_label: int = IGNORE_LABEL, full_pc: bool = False, full_res: bool = False):
        super(STPLS3DTracker, self).__init__(dataset, stage, wandb_log, use_tensorboard, ignore_label)
        self.full_pc = full_pc
        self.full_res = full_res
        self.gt_tot = None
        self.pred_tot = None

    def reset(self, *args, **kwargs):
        super().reset(*args, **kwargs)
        if self._stage == 'test':
            self._ds = self._dataset.test_data
        elif self._stage == 'val':
            self._ds = self._dataset.val_data
        else:
            self._ds = self._dataset.train_data
        self._areas = [None] * self._ds.size()
        self._metric_per_areas = [None] * self._ds.size()
        self.gt_tot = None
        self.pred_tot = None

    def track(self, model: model_interface.TrackerInterface, data=None, full_pc=False, **kwargs):
        """ Add current model predictions (usually the result of a batch) to the tracking
        """
        super().track(model)

        # Train mode or low res, nothing special to do
        if self._stage == "train" or not full_pc: #not full_pc:
            return
        inputs = data if data is not None else model.get_input()
        inputs.pred = model.get_output()[0]
        data_l = inputs.to_data_list(0)
        num_class_pred = self._num_classes - 1

        for p in range(len(data_l)):
            area_sel = data_l[p].area
            # Test mode, compute votes in order to get full res predictions
            if self._areas[area_sel] is None:
                pair = self._ds._load_save(area_sel)
                self._areas[area_sel] = pair
                if self._areas[area_sel].y is None:
                    raise ValueError("It seems that the test area data does not have labels (attribute y).")
                self._areas[area_sel].prediction_count = torch.zeros(self._areas[area_sel].y.shape[0], dtype=torch.int)
                self._areas[area_sel].votes = torch.zeros((self._areas[area_sel].y.shape[0], num_class_pred), dtype=torch.float)

                self._areas[area_sel].prediction_count_target = torch.zeros(self._areas[area_sel].y_target.shape[0], dtype=torch.int)
                self._areas[area_sel].votes_target = torch.zeros((self._areas[area_sel].y_target.shape[0], num_class_pred), dtype=torch.float)
                self._areas[area_sel].to(model.device)

            # Gather origin ids and check that it fits with the test set
            if data_l[p].idx is None:
                raise ValueError("The inputs given to the model do not have a idx_target attribute.")
            if data_l[p].idx_target is None:
                raise ValueError("The inputs given to the model do not have a idx_target attribute.")

            idx = data_l[p].idx
            if idx.dim() == 2:
                idx = idx.flatten()
            if idx.max() >= self._areas[area_sel].pos.shape[0]:
                raise ValueError("Origin ids are larger than the number of points in the original point cloud.") \

            idx_target = data_l[p].idx_target
            if idx_target.dim() == 2:
                idx_target = idx_target.flatten()
            if idx_target.max() >= self._areas[area_sel].pos_target.shape[0]:
                raise ValueError("Origin ids are larger than the number of points in the original point cloud.")
            # Set predictions
            self._areas[area_sel].votes[idx] += data_l[p].pred
            self._areas[area_sel].votes_target[idx_target] += data_l[p].pred1
            self._areas[area_sel].prediction_count[idx] += 1
            self._areas[area_sel].prediction_count_target[idx_target] += 1

    def finalise(self, save_pc=False, name_test="", saving_path=None, conv_classes=None, num_class_cm=None, **kwargs):
        per_class_iou = self._confusion_matrix.get_intersection_union_per_class()[0]
        self._iou_per_class = {INV_OBJECT_LABEL[k]: v for k, v in enumerate(per_class_iou)}
        if self.full_pc:
            if num_class_cm is None:
                if conv_classes is None:
                    num_class_cm = self._num_classes
                else:
                    num_class_cm = np.max(conv_classes)

            gt_tot, gt_tot_target = [], []
            pred_tot, pred_tot_target = [], []
            for i, area in enumerate(self._areas):
                if area is not None:
                    # Complete for points that have a prediction
                    area = area.to("cpu")
                    has_prediction = area.prediction_count > 0
                    has_prediction_target = area.prediction_count_target > 0
                    pred = torch.argmax(area.votes[has_prediction], 1)
                    pred_target = torch.argmax(area.votes_target[has_prediction_target], 1)
                    probability = torch.max(area.votes[has_prediction], 1)[0]
                    probability_target = torch.max(area.votes_target[has_prediction_target], 1)[0]
                    if conv_classes is not None:
                        pred = torch.from_numpy(conv_classes[pred])
                        pred_target = torch.from_numpy(conv_classes[pred_target])

                    pos = area.pos[has_prediction]
                    pos_target = area.pos_target[has_prediction_target]
                    c = ConfusionMatrix(num_class_cm)
                    # If full res, knn interpolation

                    if self.full_res:
                        area_orig_pos, area_orig_rgb, area_orig_gt, area_target_pos, area_target_rgb, area_target_gt = self._ds.clouds_loader(i)
                        # still on GPU no need for num_workers
                        pred = knn_interpolate(torch.unsqueeze(pred, 1), pos, area_orig_pos, k=1).numpy()
                        pred = np.squeeze(pred)
                        pred = pred.astype(int)

                        pred_target = knn_interpolate(torch.unsqueeze(pred_target, 1), pos_target, area_target_pos, k=1).numpy()
                        pred_target = np.squeeze(pred_target)
                        pred_target = pred_target.astype(int)

                        probability = knn_interpolate(torch.unsqueeze(probability, 1), pos, area_orig_pos, k=1).numpy()
                        probability = np.squeeze(probability)
                        probability = probability.astype(float)

                        probability_target = knn_interpolate(torch.unsqueeze(probability_target, 1), pos_target, area_target_pos, k=1).numpy()
                        probability_target = np.squeeze(probability_target)
                        probability_target = probability_target.astype(float)

                        gt = area_orig_gt.numpy()
                        pos = area_orig_pos

                        gt_target = area_target_gt.numpy()
                        pos_target = area_target_pos
                    else:
                        pred = pred.numpy()
                        gt = area.y[has_prediction].numpy()
                        pos = pos.cpu()

                        pred_target = pred_target.numpy()
                        gt_target = area.y_target[has_prediction_target].numpy()
                        pos_target = pos_target.cpu()

                    gt_tot.append(gt)
                    gt_tot_target.append(gt_target)
                    pred_tot.append(pred)
                    pred_tot_target.append(pred_target)
                    # Metric computation
                    c.count_predicted_batch_list(gt, pred, gt_target, pred_target)# c.count_predicted_batch(gt, pred)
                    acc = 100 * c.get_overall_accuracy()
                    macc = 100 * c.get_mean_class_accuracy()
                    miou = 100 * c.get_average_intersection_union()
                    class_iou, present_class = c.get_intersection_union_per_class()
                    class_acc = c.confusion_matrix.diagonal()/c.confusion_matrix.sum(axis=1)
                    iou_per_class = {
                        k: "{:.2f}".format(100 * v)
                        for k, v in enumerate(class_iou)
                    }
                    acc_per_class = {
                        k: "{:.2f}".format(100 * v)
                        for k, v in enumerate(class_acc)
                    }
                    miou_ch = 100 * np.mean(class_iou[1:])
                    metrics = {}
                    metrics["{}_acc".format(self._stage)] = acc
                    metrics["{}_macc".format(self._stage)] = macc
                    metrics["{}_miou".format(self._stage)] = miou
                    metrics["{}_miou_ch".format(self._stage)] = miou_ch
                    metrics["{}_iou_per_class".format(self._stage)] = iou_per_class
                    metrics["{}_acc_per_class".format(self._stage)] = acc_per_class
                    self._metric_per_areas[i] = metrics


                    if (self._stage == 'test' or self._stage == 'val') and save_pc:
                        print('Saving PC %s' % (str(i)))
                        if saving_path is None:
                            saving_path = os.path.join(os.getcwd(), name_test)
                        saving_dir = self._ds.filesPC0[i].split('/')[-2]
                        saving_p = saving_path + saving_dir
                        if not os.path.exists(saving_p):
                            os.makedirs(saving_p)

                        self._dataset.to_ply(pos, pred, os.path.join(saving_p, "pointCloud0.ply"), sf=probability)
                        self._dataset.to_ply(pos_target, pred_target, os.path.join(saving_p, "pointCloud1.ply"),sf=probability_target)
            self.gt_tot = np.concatenate(gt_tot)
            self.gt_tot_target = np.concatenate(gt_tot_target)
            self.pred_tot = np.concatenate(pred_tot)
            self.pred_tot_target = np.concatenate(pred_tot_target)
            c = ConfusionMatrix(num_class_cm)
            c.count_predicted_batch_list(self.gt_tot, self.pred_tot, self.gt_tot_target, self.pred_tot_target) # c.count_predicted_batch(self.gt_tot, self.pred_tot)
            acc = 100 * c.get_overall_accuracy()
            macc = 100 * c.get_mean_class_accuracy()
            miou = 100 * c.get_average_intersection_union()
            class_iou, present_class = c.get_intersection_union_per_class()
            iou_per_class = {
                k: "{:.2f}".format(100 * v)
                for k, v in enumerate(class_iou)
            }
            class_acc = c.confusion_matrix.diagonal() / c.confusion_matrix.sum(axis=1)
            acc_per_class = {
                k: "{:.2f}".format(100 * v)
                for k, v in enumerate(class_acc)
            }
            miou_ch = 100 * np.mean(class_iou[1:])
            self.metric_full_cumul = {"acc": acc, "macc": macc, "mIoU": miou, "miou_ch": miou_ch,
                                      "IoU per class": iou_per_class, "acc_per_class": acc_per_class}

            saving_path = os.path.join(os.getcwd(), self._stage, name_test)
            if not os.path.exists(saving_path):
                os.makedirs(saving_path)

            name_classes = [name for name, i in self._ds.class_labels.items()]
            self.save_metrics(name_test=name_test, saving_path=saving_path)
            try:
                self.plot_confusion_matrix(gt_tot, pred_tot, normalize=True, saving_path=saving_path + "cm.png",
                                           name_classes=name_classes)
            except:
                pass
            try:
                self.plot_confusion_matrix(gt_tot_target, pred_tot_target, normalize=True, saving_path=saving_path + "cm2.png")
            except:
                pass

    def get_metrics(self, verbose=False) -> Dict[str, Any]:
        """ Returns a dictionnary of all metrics and losses being tracked
        """
        metrics = super().get_metrics(verbose)

        if verbose:
            if self.full_pc:
                for i, area in enumerate(self._areas):
                    if area is not None:
                        metrics["%s_whole_pc_%s" % (self._stage, str(i) + "_" + osp.basename(self._ds.filesPC0[i]))] = \
                        self._metric_per_areas[i]
        return metrics

    def save_metrics(self, saving_path=None, name_test=""):
        metrics = self.get_metrics()
        if self.full_pc:
            for i, area in enumerate(self._areas):
                if area is not None:
                    metrics["%s_whole_pc_%s" % (self._stage, osp.basename(self._ds.filesPC0[i]))] = \
                    self._metric_per_areas[i]
        self._avg_metrics_full_pc = merge_avg_mappings(self._metric_per_areas)
        print("Average full pc res :\n")
        print(self._avg_metrics_full_pc)
        if saving_path is None:
            saving_path = os.path.join(os.getcwd(), self._stage, name_test)
        with open(osp.join(saving_path, "res.txt"), "w") as fi:
            for met, val in metrics.items():
                fi.write(met + " : " + str(val) + "\n")
            fi.write("\n")
            fi.write("Average full pc res \n")
            for met, val in self._avg_metrics_full_pc.items():
                fi.write(met + " : " + str(val) + "\n")
            fi.write("\n")
            fi.write("Cumulative full pc res \n")
            for met, val in self.metric_full_cumul.items():
                fi.write(met + " : " + str(val) + "\n")

    def plot_confusion_matrix(self, y_true, y_pred,
                              normalize=False,
                              title=None,
                              cmap=plt.cm.Blues,
                              saving_path="",
                              name_classes=None):
        """
        This function prints and plots the confusion matrix.
        Normalization can be applied by setting `normalize=True`.
        """
        if not title:
            if normalize:
                title = 'Normalized confusion matrix'
            else:
                title = 'Confusion matrix, without normalization'

        # Compute confusion matrix
        cm = confusion_matrix(y_true, y_pred)
        # Only use the labels that appear in the data
        classes = unique_labels(y_true, y_pred)
        # classes = classes[unique_labels(y_true, y_pred)]
        if normalize:
            cm = cm.astype('float') / cm.sum(axis=1)[:, np.newaxis]
            print("Normalized confusion matrix")
        else:
            print('Confusion matrix, without normalization')

        fig, ax = plt.subplots()
        im = ax.imshow(cm, interpolation='nearest', cmap=cmap)
        ax.figure.colorbar(im, ax=ax)
        # We want to show all ticks...
        if name_classes == None:
            name_classes = classes
        ax.set(xticks=np.arange(cm.shape[1]),
               yticks=np.arange(cm.shape[0]),
               # ... and label them with the respective list entries
               xticklabels=name_classes, yticklabels=name_classes,
               title=title,
               ylabel='True label',
               xlabel='Predicted label')

        # Rotate the tick labels and set their alignment.
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right",
                 rotation_mode="anchor")

        # Loop over data dimensions and create text annotations.
        fmt = '.2f' if normalize else 'd'
        thresh = cm.max() / 2.
        for i in range(cm.shape[0]):
            for j in range(cm.shape[1]):
                ax.text(j, i, format(cm[i, j], fmt),
                        ha="center", va="center",
                        color="white" if cm[i, j] > thresh else "black")
        fig.tight_layout()
        plt.xlim(-0.5, len(np.unique(y_pred)) - 0.5)
        plt.ylim(len(np.unique(y_pred)) - 0.5, -0.5)
        if saving_path != "":
            plt.savefig(saving_path)
        return ax


def merge_avg_mappings(dicts):
    """ Merges an arbitrary number of dictionaries based on the
    average value in a given mapping.

    Parameters
    ----------
    dicts : Dict[Any, Comparable]

    Returns
    -------
    Dict[Any, Comparable]
        The merged dictionary
    """
    merged = {}
    cpt = {}
    for d in dicts:  # `dicts` is a list storing the input dictionaries
        for key in d:
            if key not in merged:
                if type(d[key]) == dict:
                    merged[key] = {}
                    for key2 in d[key]:
                        merged[key][key2] = float(d[key][key2])
                else:
                    merged[key] = d[key]
                cpt[key] = 1
            else:
                if type(d[key]) == dict:
                    for key2 in d[key]:
                        merged[key][key2] += float(d[key][key2])
                else:
                    merged[key] += d[key]
                cpt[key] += 1
    for key in merged:
        if type(merged[key]) == dict:
            for key2 in merged[key]:
                merged[key][key2] /= cpt[key]
        else:
            merged[key] /= cpt[key]
    return merged
