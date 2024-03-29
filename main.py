from datetime import datetime
import argparse
import yaml
import dgl
import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F
import numpy as np
import matplotlib.pyplot as plt
from utils.preprocess_DBLP import DBLP4057Dataset, DBLPFourAreaDataset
from utils.preprocess_ACM import ACMDataset
from utils.preprocess_HeCo import DBLPHeCoDataset, ACMHeCoDataset, AMinerHeCoDataset, FreebaseHeCoDataset
from utils.evaluate import score, LogisticRegression, MLP
from models.HGAE import HGAE
from models.HAN import HAN
from tqdm import tqdm


device_0 = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
device_1 = torch.device("cuda:1" if torch.cuda.is_available() else "cpu")
heterogeneous_dataset = {
    "dblp": DBLPFourAreaDataset,
    "acm": ACMDataset,
    "heco_acm": {"name": ACMHeCoDataset, "relations": [("author", "ap", "paper")]},
    "heco_dblp": {
        "name": DBLPHeCoDataset,
        # 'relations': [('paper', 'pa', 'author'), ('paper', 'pt', 'term'),(('paper', 'pc', 'conference'))]
        "relations": [
            ("paper", "pa", "author"),
        ],
    },
    "heco_freebase": {"name": FreebaseHeCoDataset, "relations": [("author", "am", "movie"), ("director", "dm", "movie"), ("writer", "wm", "movie")]},
    "heco_aminer": {"name": AMinerHeCoDataset, "relations": [("paper", "pa", "author"), ("paper", "pr", "reference")]},
}


def load_config(args, path):
    with open(path, "r") as f:
        config = yaml.load(f, yaml.FullLoader)
    config = config[args.dataset]

    for k, v in config.items():
        setattr(args, k, v)
    return args


def node_classification_evaluate(enc_feat, args, num_classes, labels, train_mask, val_mask, test_mask, ratio):
    classifier = MLP(num_dim=args.num_hidden, num_classes=num_classes)
    classifier = classifier.to(device_1)
    optimizer = optim.Adam(classifier.parameters(), lr=args.eva_lr, weight_decay=args.eva_wd)
    enc_feat = enc_feat.to(device_1)
    emb = {"train": enc_feat[train_mask[ratio]], "val": enc_feat[val_mask[ratio]], "test": enc_feat[test_mask[ratio]]}
    labels = {"train": labels[train_mask[ratio]], "val": labels[val_mask[ratio]], "test": labels[test_mask[ratio]]}

    val_macro = []
    val_micro = []
    val_accuracy = []
    best_val_acc = 0.0
    best_model_state_dict = None

    for epoch in tqdm(range(args.eva_epoches), position=0):
        classifier.train()
        train_output = classifier(emb["train"]).cpu()
        eva_loss = F.cross_entropy(train_output, labels["train"])
        optimizer.zero_grad()
        eva_loss.backward(retain_graph=True)
        optimizer.step()

        with torch.no_grad():
            classifier.eval()
            val_output = classifier(emb["val"]).cpu()
            val_acc, val_micro_f1, val_macro_f1 = score(val_output, labels["val"])

            if val_acc > best_val_acc:
                best_val_acc = val_acc
                best_model_state_dict = classifier.state_dict()

            val_accuracy.append(val_acc)
            val_micro.append(val_micro_f1)
            val_macro.append(val_macro_f1)

    classifier.load_state_dict(best_model_state_dict)
    test_output = classifier(emb["test"]).cpu()
    test_acc, test_micro_f1, test_macro_f1 = score(test_output, labels["test"])

    return test_acc, test_micro_f1, test_macro_f1
    # return max(val_accuracy), max(val_micro), max(val_macro)


def train(args):
    data = heterogeneous_dataset[args.dataset]["name"]()
    metapaths = data.metapaths
    relations = heterogeneous_dataset[args.dataset]["relations"]
    graph = data[0]
    sc_subgraphs = [graph[rel].to(device_0) for rel in relations]
    mp_subgraphs = [dgl.metapath_reachable_graph(graph, metapath).to(device_0) for metapath in metapaths]  # homogeneous graph divide by metapaths
    all_types = list(data._ntypes.values())
    ntype = data.predict_ntype
    num_classes = data.num_classes
    features = {t: graph.nodes[t].data["feat"].to(device_0) for t in all_types if "feat" in graph.nodes[t].data}
    ntype_labels = graph.nodes[ntype].data["label"]
    label_ratio = ["20", "40", "60"]
    train_mask = {ratio: graph.nodes[ntype].data[f"train_mask_{ratio}"] for ratio in label_ratio}
    val_mask = {ratio: graph.nodes[ntype].data[f"val_mask_{ratio}"] for ratio in label_ratio}
    test_mask = {ratio: graph.nodes[ntype].data[f"test_mask_{ratio}"] for ratio in label_ratio}

    # model = HAN(num_metapaths=len(metapaths), in_dim=features[ntype].shape[1], hidden_dim=args.num_hidden, out_dim=num_classes,
    #            num_heads=args.num_heads, dropout=args.dropout)
    model = HGAE(
        num_metapath=len(metapaths),
        num_relations=len(relations),
        target_in_dim=features[ntype].shape[1],
        all_in_dim=[feat.shape[1] for feat in features.values()],
        args=args,
    )
    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    if args.scheduler:
        # scheduler = torch.optim.lr_scheduler.ExponentialLR(
        #     optimizer, gamma=0.99)

        def scheduler(epoch):
            return (1 + np.cos((epoch) * np.pi / args.epoches)) * 0.5

        # scheduler = lambda epoch: epoch / warmup_steps if epoch < warmup_steps \
        # else ( 1 + np.cos((epoch - warmup_steps) * np.pi / (max_epoch - warmup_steps))) * 0.5
        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=scheduler)
    model = model.to(device_0)
    performance = {
        "20": {"Acc": [], "Macro-F1": [], "Micro-F1": []},
        "40": {"Acc": [], "Macro-F1": [], "Micro-F1": []},
        "60": {"Acc": [], "Macro-F1": [], "Micro-F1": []},
    }
    log_times = datetime.now().strftime("[%Y-%m-%d_%H:%M:%S]")
    for epoch in range(args.epoches):
        model.train()
        features[ntype] = features[ntype].to(device_0)

        loss = model(mp_subgraphs, sc_subgraphs, features, ntype)

        optimizer.zero_grad()
        # output = model(mp_subgraphs, features[ntype])
        # output = output.cpu()
        # loss = F.cross_entropy(output[train_mask], ntype_labels[train_mask])
        loss.backward()
        optimizer.step()
        scheduler.step()

        print(f"Epoch:{epoch} Training Loss:{loss.item()} learning_rate={scheduler.get_last_lr()}")
        if epoch > 0 and (epoch + 1) % 20 == 0:
            with open(f"./log/{args.encoder}+{args.decoder}_{args.dataset}_{log_times}.txt", "a") as log_file:
                result_acc = []
                result_micro = []
                result_macro = []
                log_file.write(f"Epoches:{epoch}-----------------------------------\n")
                for ratio in label_ratio:
                    features[ntype] = features[ntype].to(device_0)
                    if args.encoder == "HAN":
                        enc_feat = model.encoder(mp_subgraphs, features[ntype])
                    elif args.encoder == "SRN":
                        enc_feat = model.encoder(sc_subgraphs, features[ntype], features)
                    elif args.encoder == "HAN_SRN":
                        enc_feat = model.encoder(mp_subgraphs, sc_subgraphs, features[ntype], features)

                    max_acc, max_micro, max_macro = node_classification_evaluate(
                        enc_feat, args, num_classes, ntype_labels, train_mask, val_mask, test_mask, ratio
                    )
                    performance[ratio]["Acc"].append(max_acc)
                    performance[ratio]["Macro-F1"].append(max_macro)
                    performance[ratio]["Micro-F1"].append(max_micro)
                    result_acc.append(max_acc)
                    result_micro.append(max_micro)
                    result_macro.append(max_macro)
                    log_file.write("\t Label Rate:{}% [Accuracy:{:4f} Macro-F1:{:4f} Micro-F1:{:4f} ]\n".format(ratio, max_acc, max_macro, max_micro))

    fig, axs = plt.subplots(1, len(label_ratio), figsize=(15, 5))
    x_range = list(range(0, args.epoches + 1, 20))[1:]
    for i, ratio in enumerate(label_ratio):
        axs[i].set_title(f"Label Rate {ratio}%")
        axs[i].plot(x_range, performance[ratio]["Acc"], label="Acc")
        axs[i].plot(x_range, performance[ratio]["Macro-F1"], label="Macro-F1")
        axs[i].plot(x_range, performance[ratio]["Micro-F1"], label="Micro-F1")
        axs[i].legend()
        axs[i].set_xlabel("epoch")
    formatted_now = datetime.now().strftime("[%Y-%m-%d_%H:%M:%S]")
    fig.savefig(f"./img/{args.encoder}+{args.decoder}_{args.dataset}_{formatted_now}.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser("Heterogeneous Project")
    parser.add_argument("--devices", type=int, default=0)
    parser.add_argument("--dataset", type=str, default="dblp")
    parser.add_argument("--epoches", type=int, default=200)
    parser.add_argument("--eva_epoches", type=int, default=50)
    parser.add_argument("--num_layer", type=int, default=3, help="number of model layer")
    parser.add_argument("--num_heads", type=int, default=8, help="number of attention heads")
    parser.add_argument("--num_out_heads", type=int, default=1, help="number of attention output heads")
    parser.add_argument("--num_hidden", type=int, default=256, help="number of hidden units")
    parser.add_argument("--dropout", type=float, default=0.4, help="dropout probability")
    parser.add_argument("--lr", type=float, default=0.001, help="learning rate")
    parser.add_argument("--mask_rate", type=float, default=0.5, help="masked node rates")
    parser.add_argument("--encoder", type=str, default="HAN", help="heterogeneous encoder")
    parser.add_argument("--decoder", type=str, default="HAN", help="Heterogeneous decoder")
    parser.add_argument("--weight_decay", type=float, default=1e-4, help="weight decay")
    parser.add_argument("--gamma", type=int, default=3, help="gamma for cosine similarity")
    parser.add_argument("--scheduler", default=True, help="scheduler for optimizer")
    parser.add_argument("--use_config", default=True, help="use best parameter in config.yaml ")
    args = parser.parse_args()

    if args.use_config:
        args = load_config(args, "config.yaml")
    print(args)

    device_0 = torch.device(f"cuda:{args.devices}" if torch.cuda.is_available() else "cpu")
    device_1 = torch.device(f"cuda:{args.devices ^ 1}" if torch.cuda.is_available() else "cpu")
    train(args=args)
