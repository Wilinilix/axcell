import dataclasses
from dataclasses import dataclass
import json
from pathlib import Path
import numpy as np
import pandas as pd
from sota_extractor2.models.structure.nbsvm import *
from sklearn.metrics import confusion_matrix
from matplotlib  import pyplot as plt
import seaborn as sn
from enum import Enum
import pickle

class Labels(Enum):
    OTHER=0
    DATASET=1
    PAPER_MODEL=2
    COMPETING_MODEL=3

label_map = {
    "dataset": Labels.DATASET.value,
    "dataset-sub": Labels.DATASET.value,
    "model-paper": Labels.PAPER_MODEL.value,
    "model-best": Labels.PAPER_MODEL.value,
    "model-competing": Labels.COMPETING_MODEL.value
}

# put here to avoid recompiling, used only in _limit_context
elastic_tag_split_re = re.compile("(<b>.*?</b>)")

@dataclass
class Experiment:
    vectorizer: str = "tfidf"
    this_paper: bool = False
    merge_fragments: bool = False
    merge_type: str = "concat"  # "concat", "vote_maj", "vote_avg", "vote_max"
    evidence_source: str = "text"  # "text" or "text_highlited"
    split_btags: bool = False  # <b>Test</b> -> <b> Test </b>
    fixed_tokenizer: bool = False  # if True, <b> and </b> are not split into < b > and < / b >
    fixed_this_paper: bool = False # if True and this_paper, filter this_paper before merging fragments
    mask: bool = False             # if True and evidence_source = "text_highlited", replace <b>...</b> with xxmask
    evidence_limit: int = None     # maximum number of evidences per cell (grouped by (ext_id, this_paper))
    context_tokens: int = None      # max. number of words before <b> and after </b>
    analyzer: str = "word"            # "char", "word" or "char_wb"
    lowercase: bool = True

    class_weight: str = None
    multinomial_type: str = "manual"  # "manual", "ovr", "multinomial"
    solver: str = "liblinear"  # 'lbfgs' - large, liblinear for small datasets
    C: float = 4.0
    dual: bool = True
    penalty: str = "l2"
    ngram_range: tuple = (1, 2)
    min_df: int = 3
    max_df: float = 0.9
    max_iter: int = 1000

    results: dict = dataclasses.field(default_factory=dict)

    has_model: bool = False     # either there's already pretrained model or it's a saved experiment and there's a saved model as well
    name: str = None

    def _get_next_exp_name(self, dir_path):
        dir_path = Path(dir_path)
        files = [f.name for f in dir_path.glob("*.exp.json")]
        for i in range(100000):
            name = f"{i:05d}.exp.json"
            if name not in files:
                return dir_path / name
        raise Exception("You have too many files in this dir, really!")

    def _save_model(self, path):
        with open(path, 'wb') as f:
            pickle.dump(self._model, f)

    def _load_model(self, path):
        with open(path, 'rb') as f:
            self._model = pickle.load(f)
            return self._model

    def load_model(self):
        path = self._path.parent / f"{self._path.stem}.model"
        return self._load_model(path)

    def save(self, dir_path):
        dir_path = Path(dir_path)
        dir_path.mkdir(exist_ok=True, parents=True)
        filename = self._get_next_exp_name(dir_path)
        j = dataclasses.asdict(self)
        with open(filename, "wt") as f:
            json.dump(j, f)
        if hasattr(self, "_model"):
            fn = filename.stem
            self._save_model(dir_path / f"{fn}.model")
        return filename.name

    def to_df(self):
        d = dataclasses.asdict(self)
        res = d.pop("results")
        d.update(res)
        row = pd.DataFrame({k: [v] for k, v in d.items()})
        return row

    def new_experiment(self, **kwargs):
        # reset this fields unless their provided in load()
        kwargs.setdefault("has_model", False)
        kwargs.setdefault("results", {})
        return dataclasses.replace(self, **kwargs)

    def update_results(self, **kwargs):
        self.results.update(**kwargs)

    def get_trained_model(self, train_df):
        nbsvm = NBSVM(experiment=self)
        nbsvm.fit(train_df["text"], train_df["label"])
        self._model = nbsvm
        self.has_model = True
        return nbsvm

    def _limit_context(self, text):
        parts = elastic_tag_split_re.split(text)
        new_parts = []
        end = len(parts)
        for i, part in enumerate(parts):
            if i % 2 == 0:
                toks = tokenize(part)
                if i == 0:
                    toks = toks[-self.context_tokens:]
                elif i == end:
                    toks = toks[:self.context_tokens]
                else:
                    j = len(toks) - 2 * self.context_tokens
                    if j > 0:
                        toks = toks[:self.context_tokens] + toks[-self.context_tokens:]
                new_parts.append(' '.join(toks))
            else:
                new_parts.append(part)
        return ' '.join(new_parts)



    def _transform_df(self, df):
        if self.merge_type not in ["concat", "vote_maj", "vote_avg", "vote_max"]:
            raise Exception(f"merge_type must be one of concat, vote_maj, vote_avg, vote_max, but {self.merge_type} was given")
        #df = df[df["cell_type"] != "table-meta"]  # otherwise we get precision 0 on test set
        if self.evidence_limit is not None:
            df = df.groupby(by=["ext_id", "this_paper"]).head(self.evidence_limit)
        if self.context_tokens is not None:
            df.loc["text_highlited"] = df["text_highlited"].apply(self._limit_context)
            df.loc["text"] = df["text_highlited"].str.replace("<b>", " ").replace("</b>", " ")
        if self.evidence_source != "text":
            df = df.copy(True)
            if self.mask:
                df["text"] = df[self.evidence_source].replace(re.compile("<b>.*?</b>"), " xxmask ")
            else:
                df["text"] = df[self.evidence_source]

        elif self.mask:
            raise Exception("Masking with evidence_source='text' makes no sense")
        if not self.fixed_this_paper:
            if self.merge_fragments and self.merge_type == "concat":
                df = df.groupby(by=["ext_id", "cell_content", "cell_type", "this_paper"]).text.apply(
                    lambda x: "\n".join(x.values)).reset_index()
            df = df.drop_duplicates(["text", "cell_content", "cell_type"]).fillna("")
            if self.this_paper:
                df = df[df.this_paper]
        else:
            if self.this_paper:
                df = df[df.this_paper]
            if self.merge_fragments and self.merge_type == "concat":
                df = df.groupby(by=["ext_id", "cell_content", "cell_type"]).text.apply(
                    lambda x: "\n".join(x.values)).reset_index()
            df = df.drop_duplicates(["text", "cell_content", "cell_type"]).fillna("")

        if self.split_btags:
            df["text"] = df["text"].replace(re.compile(r"(\</?b\>)"), r" \1 ")
        df = df.replace(re.compile(r"(xxref|xxanchor)-[\w\d-]*"), "\\1 ")
        df = df.replace(re.compile(r"(^|[ ])\d+\.\d+(\b|%)"), " xxnum ")
        df = df.replace(re.compile(r"(^|[ ])\d+(\b|%)"), " xxnum ")
        df = df.replace(re.compile(r"\bdata set\b"), " dataset ")
        df["label"] = df["cell_type"].apply(lambda x: label_map.get(x, 0))
        df["label"] = pd.Categorical(df["label"])
        return df

    def transform_df(self, *dfs):
        transformed = [self._transform_df(df) for df in dfs]
        if len(transformed) == 1:
            return transformed[0]
        return transformed

    def _set_results(self, prefix, preds, true_y):
        m = metrics(preds, true_y)
        r = {}
        r[f"{prefix}_accuracy"] = m["accuracy"]
        r[f"{prefix}_precision"] = m["precision"]
        r[f"{prefix}_cm"] = confusion_matrix(true_y, preds).tolist()
        self.update_results(**r)

    def evaluate(self, model, train_df, valid_df, test_df):
        for prefix, tdf in zip(["train", "valid", "test"], [train_df, valid_df, test_df]):
            probs = model.predict_proba(tdf["text"])
            preds = np.argmax(probs, axis=1)

            if self.merge_fragments and self.merge_type != "concat":
                if self.merge_type == "vote_maj":
                    vote_results = preds_for_cell_content(tdf, probs)
                elif self.merge_type == "vote_avg":
                    vote_results = preds_for_cell_content_multi(tdf, probs)
                elif self.merge_type == "vote_max":
                    vote_results = preds_for_cell_content_max(tdf, probs)
                preds = vote_results["pred"]
                true_y = vote_results["true"]
            else:
                true_y = tdf["label"]
            self._set_results(prefix, preds, true_y)

    def show_results(self, *ds):
        if not len(ds):
            ds = ["train", "valid", "test"]
        for prefix in ds:
            print(f"{prefix} dataset")
            print(f" * accuracy: {self.results[f'{prefix}_accuracy']}")
            print(f" * precision: {self.results[f'{prefix}_precision']}")
            self._plot_confusion_matrix(np.array(self.results[f'{prefix}_cm']), normalize=True)

    def _plot_confusion_matrix(self, cm, normalize):
        if normalize:
            cm = cm / cm.sum(axis=1)[:, None]
        target_names = ["OTHER", "DATASET", "MODEL (paper)", "MODEL (comp.)"]
        df_cm = pd.DataFrame(cm, index=[i for i in target_names],
                             columns=[i for i in target_names])
        plt.figure(figsize=(10, 10))
        ax = sn.heatmap(df_cm,
                        annot=True,
                        square=True,
                        fmt="0.2f" if normalize else "d",
                        cmap="YlGnBu",
                        mask=cm == 0,
                        linecolor="black",
                        linewidths=0.01)
        ax.set_ylabel("True")
        ax.set_xlabel("Predicted")

    @classmethod
    def load_all(cls, dir_path):
        dir_path = Path(dir_path)
        return [cls.load(f) for f in dir_path.glob("*.exp.json")]

    @classmethod
    def load(cls, path):
        # a new field added to the class should not change
        # the default behaviour of experiment, so that we
        # can load older experiments by setting missing fields
        # to their default values
        e = cls()
        path = Path(path)
        with open(path, "rt") as f:
            j = json.load(f)
        j["name"] = path.name
        e = e.new_experiment(**j)
        e._path = path
        return e

    @classmethod
    def experiments_to_df(cls, exps):
        dfs = [e.to_df() for e in exps]
        df = pd.concat(dfs)
        return df