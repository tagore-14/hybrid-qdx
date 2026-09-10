import json
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import yaml
from sklearn.model_selection import train_test_split

from .data import DATA_LOADERS
from .data.base import Dataset
from .evaluation import evaluate_model
from .explain import explain_model
from .models import MODEL_REGISTRY
from .preprocessing import QuantumReadyPreprocessor
from .qaoa_feature_selection import QAOAFeatureSelector

ProgressCallback = Optional[Callable[[str, dict], None]]

PREPROCESSOR_REGISTRY = {
    "pca": lambda n_components, **kw: QuantumReadyPreprocessor(n_components=n_components),
    "qaoa_feature_select": lambda n_components, **kw: QAOAFeatureSelector(n_components=n_components, **kw),
}


def _safe_train_test_split(X, y, test_size, random_state):
    """Split small custom datasets without stratify failing on rare classes."""
    labels, counts = np.unique(y, return_counts=True)
    if len(labels) < 2:
        raise ValueError("The diagnosis column must contain at least two classes.")

    n_test = max(1, int(np.ceil(len(y) * test_size))) if isinstance(test_size, float) else int(test_size)
    n_train = len(y) - n_test
    can_stratify = counts.min() >= 2 and n_test >= len(labels) and n_train >= len(labels)
    if can_stratify:
        return train_test_split(
            X, y, test_size=test_size, random_state=random_state, stratify=y
        )

    train_idx, test_idx = train_test_split(
        np.arange(len(y)), test_size=test_size, random_state=random_state
    )
    if len(np.unique(y[train_idx])) < 2:
        missing_labels = set(labels) - set(np.unique(y[train_idx]))
        swap_candidates = [i for i in test_idx if y[i] in missing_labels]
        if not swap_candidates:
            raise ValueError("The dataset does not have enough samples from both classes for training.")
        swap_in = swap_candidates[0]
        swap_out = train_idx[0]
        train_idx = np.array([swap_in if i == swap_out else i for i in train_idx])
        test_idx = np.array([swap_out if i == swap_in else i for i in test_idx])

    return X[train_idx], X[test_idx], y[train_idx], y[test_idx]


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def run_on_dataset(
    dataset: Dataset, config: dict, verbose: bool = True, progress_cb: ProgressCallback = None
) -> dict:
    """Run the benchmark pipeline on an already-loaded Dataset.

    Used both by the config/CLI path (dataset comes from a registered
    DataLoader) and by the interactive dashboard path (dataset is built at
    runtime from an uploaded CSV) - the actual train/preprocess/evaluate
    logic is identical either way.
    """

    def notify(event: str, payload: dict):
        if progress_cb:
            progress_cb(event, payload)

    test_size = config.get("test_size", 0.25)
    random_state = config.get("random_state", 42)

    X_train, X_test, y_train, y_test = _safe_train_test_split(
        dataset.X, dataset.y, test_size=test_size, random_state=random_state
    )

    prep_cfg = dict(config["preprocessing"])
    n_components = prep_cfg.pop("n_components")
    prep_method = prep_cfg.pop("method", "pca")
    if prep_method not in PREPROCESSOR_REGISTRY:
        raise ValueError(f"Unknown preprocessing method '{prep_method}'. Available: {list(PREPROCESSOR_REGISTRY)}")

    preprocessor = PREPROCESSOR_REGISTRY[prep_method](n_components=n_components, **prep_cfg)
    X_train_proc = preprocessor.fit_transform(X_train, y_train, feature_names=dataset.feature_names)
    X_test_proc = preprocessor.transform(X_test)
    prep_summary = preprocessor.summary()

    if verbose:
        print(f"Dataset: {dataset.name} | train={len(X_train)} test={len(X_test)} "
              f"| raw_features={dataset.X.shape[1]} -> qubits/components={n_components} "
              f"| preprocessing={prep_method}")
        print(f"Preprocessing summary: {prep_summary}")
    notify("dataset_ready", {
        "n_train": len(X_train), "n_test": len(X_test), "preprocessing_summary": prep_summary,
    })

    results = []
    explanations = {}
    explain_enabled = config.get("explain", False)
    n_explain = config.get("explain_samples", 20)
    explain_feature_names = prep_summary.get("selected_feature_names") or [f"pc_{i}" for i in range(n_components)]

    for spec in config["models"]:
        spec = dict(spec)
        model_type = spec.pop("type")
        if model_type not in MODEL_REGISTRY:
            raise ValueError(f"Unknown model type '{model_type}'. Available: {list(MODEL_REGISTRY)}")

        if verbose:
            print(f"\n--- Training {model_type} ---")
        notify("training_start", {"model_type": model_type})
        t0 = time.time()
        model = MODEL_REGISTRY[model_type](n_qubits=n_components, **spec)
        eval_result = evaluate_model(model, X_train_proc, y_train, X_test_proc, y_test)
        results.append(eval_result.to_dict())
        notify("training_done", {"model_type": model_type, "result": eval_result.to_dict(), "wall_seconds": time.time() - t0})

        if verbose:
            r = eval_result
            print(
                f"acc={r.accuracy:.3f} sens={r.sensitivity:.3f} spec={r.specificity:.3f} "
                f"f1={r.f1:.3f} auc={r.roc_auc} train_time={r.training_time_seconds:.2f}s "
                f"(wall {time.time() - t0:.2f}s)"
            )

        if explain_enabled:
            try:
                explanations[model_type] = explain_model(
                    model,
                    X_background=X_train_proc,
                    X_explain=X_test_proc[:n_explain],
                    feature_names=explain_feature_names,
                )
            except Exception as exc:
                explanations[model_type] = {"error": str(exc)}
            notify("explain_done", {"model_type": model_type})

    output = {
        "dataset": dataset.name,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "raw_feature_count": dataset.X.shape[1],
        "n_components": n_components,
        "preprocessing_summary": prep_summary,
        "results": results,
        "explanations": explanations,
    }

    output_path = config.get("output")
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        if verbose:
            print(f"\nSaved results to {output_path}")

    return output


def run_experiment(config: dict, verbose: bool = True, progress_cb: ProgressCallback = None) -> dict:
    dataset_name = config["dataset"]
    if dataset_name not in DATA_LOADERS:
        raise ValueError(f"Unknown dataset '{dataset_name}'. Available: {list(DATA_LOADERS)}")
    dataset = DATA_LOADERS[dataset_name]().load()
    return run_on_dataset(dataset, config, verbose=verbose, progress_cb=progress_cb)


def run_from_file(config_path: str, verbose: bool = True) -> dict:
    return run_experiment(load_config(config_path), verbose=verbose)
