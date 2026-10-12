#!/usr/bin/env python3
"""Due Random Forest in cascata sui CSV di dataset_processed.

python -m tools.train_random_forests --data-dir dataset_processed

RF 1: P(almeno un errore), usando le feature e ignorando il vecchio final_risk.
RF 2: classificazione multilabel, usando anche final_risk predetto da RF 1.
Gli eventi sovrapposti vengono separati su '+', normal equivale a nessun errore.
Unico file scritto: bundle joblib con modelli, colonne e soglie per l'inferenza.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import (average_precision_score, brier_score_loss,
    classification_report, f1_score, hamming_loss, log_loss, roc_auc_score)
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline

# ============================================================================
# SCHEMA: le label non entrano negli input; final_risk esistente viene ignorato
# ============================================================================
from shared.inference import ERROR_LABELS, prepare_features, positive_probability, predict
EXCLUDED_COLUMNS = {'timestamp', 'anomaly_label', 'final_risk'}


def encode_labels(labels):
    """Converte una label o una combinazione in cinque target binari."""
    encoded = np.zeros((len(labels), len(ERROR_LABELS)), dtype=np.uint8)
    for row, label in enumerate(labels):
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f'Label mancante/non valida alla riga {row}')
        kinds = set(label.strip().split('+'))
        if kinds == {'normal'}:
            continue
        unknown = kinds - set(ERROR_LABELS)
        if unknown:
            raise ValueError(f'Label non riconosciute: {sorted(unknown)}')
        for column, kind in enumerate(ERROR_LABELS):
            encoded[row, column] = int(kind in kinds)
    return encoded


def load_split(root, split, feature_columns=None):
    """Legge ricorsivamente i CSV mantenendo il gruppo del percorso ferroviario."""
    folder = root / split
    paths = sorted(folder.rglob('*.csv'))
    if not paths:
        raise ValueError(f'Nessun CSV in {folder}')
    frames, groups = [], []
    for path in paths:
        frame = pd.read_csv(path)
        if not {'timestamp', 'anomaly_label'} <= set(frame):
            raise ValueError(f'{path}: timestamp/anomaly_label mancanti')
        columns = [name for name in frame if name not in EXCLUDED_COLUMNS]
        if feature_columns is None:
            feature_columns = columns
        if not feature_columns or set(columns) != set(feature_columns):
            raise ValueError(f'{path}: schema feature non coerente')
        # Una cartella di rotta contiene tutte le varianti dello stesso percorso.
        relative = path.parent.relative_to(folder)
        if relative == Path('.'):
            raise ValueError(f'{path}: attesa una sottocartella per la rotta')
        frames.append(frame)
        groups.extend([relative.as_posix()] * len(frame))
    combined = pd.concat(frames, ignore_index=True)
    x = prepare_features(combined, feature_columns)
    y = encode_labels(combined.anomaly_label)
    return x, y, np.asarray(groups), feature_columns


# ============================================================================
# MODELLI: imputer e indicatori dei valori mancanti + Random Forest
# ============================================================================
def make_forest(trees=200, max_depth=12, min_samples_leaf=5, seed=42, jobs=-1):
    return make_pipeline(
        SimpleImputer(strategy='median', add_indicator=True, keep_empty_features=True),
        RandomForestClassifier(n_estimators=trees, max_depth=max_depth,
            min_samples_leaf=min_samples_leaf, random_state=seed, n_jobs=jobs),
    )


def train_models(x, targets, route_groups, folds=5, threshold=.5, **forest_options):
    """Cross-fitting per costruire l'input final_risk della seconda RF senza leakage."""
    binary = targets.any(axis=1).astype(np.uint8)
    if len(np.unique(binary)) != 2:
        raise ValueError('Il train deve contenere sia normal sia errori')
    n_groups = len(np.unique(route_groups))
    if n_groups < 2:
        raise ValueError('Servono almeno due rotte train per final_risk out-of-fold')
    n_splits = min(folds, n_groups)
    if n_splits < 2:
        raise ValueError('folds deve essere almeno 2')
    oof_risk = np.full(len(x), np.nan)
    for number, (fit_index, held_index) in enumerate(
            GroupKFold(n_splits=n_splits).split(x, binary, groups=route_groups), 1):
        if len(np.unique(binary[fit_index])) != 2:
            raise ValueError(f'Fold {number}: training privo di normal oppure errori')
        model = make_forest(**forest_options)
        model.fit(x.iloc[fit_index], binary[fit_index])
        oof_risk[held_index] = positive_probability(model, x.iloc[held_index])
        print(f'Cross-fitting RF rischio: fold {number}/{n_splits}')
    if not np.isfinite(oof_risk).all():
        raise RuntimeError('Previsioni out-of-fold incomplete')

    # RF rischio definitiva: tutto il train. Validation e test restano esclusi.
    risk_model = make_forest(**forest_options)
    risk_model.fit(x, binary)
    # RF label: final_risk ottenuto solo da modelli che non hanno visto la rotta.
    augmented = x.copy()
    augmented['final_risk'] = oof_risk
    label_model = make_forest(**forest_options)
    label_model.fit(augmented, targets)
    return dict(version=1, feature_columns=list(x.columns), error_labels=list(ERROR_LABELS),
        risk_model=risk_model, label_model=label_model, label_threshold=threshold,
        folds=n_splits, forest_options=forest_options)


# ============================================================================
# VALUTAZIONE: validation/test, metriche binarie e multilabel separate
# ============================================================================
def evaluate(models, x, targets):
    predictions = predict(models, x)
    binary = targets.any(axis=1).astype(np.uint8)
    risk = predictions.final_risk.to_numpy()
    probabilities = predictions[[f'probability_{k}' for k in ERROR_LABELS]].to_numpy()
    label_predictions = probabilities >= models['label_threshold']
    both_classes = len(np.unique(binary)) == 2
    report = dict(
        samples=len(x),
        binary=dict(brier_score=float(brier_score_loss(binary, risk)),
            log_loss=float(log_loss(binary, np.column_stack((1-risk, risk)), labels=[0, 1])),
            roc_auc=float(roc_auc_score(binary, risk)) if both_classes else None,
            average_precision=float(average_precision_score(binary, risk)) if binary.any() else None,
            f1_at_05=float(f1_score(binary, risk >= .5, zero_division=0))),
        multilabel=dict(exact_match=float(np.all(targets == label_predictions, axis=1).mean()),
            hamming_loss=float(hamming_loss(targets, label_predictions)),
            classification_report=classification_report(targets, label_predictions,
                target_names=list(ERROR_LABELS), output_dict=True, zero_division=0)),
    )
    return report


# ============================================================================
# CLI: addestramento, valutazione a console e UNICO bundle di modelli su disco
# ============================================================================
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('dataset_processed'))
    parser.add_argument('--model-path', type=Path, default=Path('models/random_forest_models.joblib'))
    parser.add_argument('--trees', type=int, default=200)
    parser.add_argument('--max-depth', type=int, default=12)
    parser.add_argument('--min-samples-leaf', type=int, default=5)
    parser.add_argument('--folds', type=int, default=5)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--jobs', type=int, default=-1)
    parser.add_argument('--label-threshold', type=float, default=.5)
    args = parser.parse_args(argv)
    if not 0 < args.label_threshold < 1:
        parser.error('label-threshold deve essere tra 0 e 1')
    splits = {}
    columns = None
    for split in ('train', 'validation', 'test'):
        x, targets, groups, columns = load_split(args.data_dir, split, columns)
        splits[split] = (x, targets, groups)
        print(f'{split}: {len(x)} campioni, {len(np.unique(groups))} rotte')
    # I nomi delle rotte identificano lo stesso percorso: vietate intersezioni fra split.
    for a, b in [('train', 'validation'), ('train', 'test'), ('validation', 'test')]:
        overlap = set(splits[a][2]) & set(splits[b][2])
        if overlap:
            raise ValueError(f'Rotte condivise fra {a} e {b}: {sorted(overlap)}')
    x, targets, groups = splits['train']
    models = train_models(x, targets, groups, folds=args.folds, threshold=args.label_threshold,
        trees=args.trees, max_depth=args.max_depth, min_samples_leaf=args.min_samples_leaf,
        seed=args.seed, jobs=args.jobs)
    for split in ('validation', 'test'):
        x, targets, _ = splits[split]
        print(f'\nValutazione {split}:')
        print(json.dumps(evaluate(models, x, targets), indent=2))
    # Unico artefatto: entrambi i modelli nello stesso file, nessuna nuova cartella di dati.
    args.model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(models, args.model_path)
    print(f'\nModelli salvati in {args.model_path}')
    print('final_risk è una probabilità stimata non calibrata, riferita al dataset sintetico.')


if __name__ == '__main__':
    main()
