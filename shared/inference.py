"""Inferenza RF binaria e multilabel, senza accesso a label o ground truth."""
import numpy as np
import pandas as pd

ERROR_LABELS = ('spoofing', 'jamming', 'low_satellites', 'multipath', 'tunnel')

def prepare_features(frame, columns):
    """Mantiene l'ordine delle colonne; imputer fit soltanto sul train del modello."""
    missing = set(columns) - set(frame)
    if missing:
        raise ValueError(f'Feature mancanti: {sorted(missing)}')
    x = frame.loc[:, columns].apply(pd.to_numeric, errors='raise')
    if np.isinf(x.to_numpy(dtype=float)).any():
        raise ValueError('Le feature contengono valori infiniti')
    return x

def positive_probability(model, x):
    """P(y=1), compreso il caso limite di un modello con una sola classe."""
    classes = model[-1].classes_
    matches = np.flatnonzero(classes == 1)
    return model.predict_proba(x)[:, matches[0]] if len(matches) else np.zeros(len(x))

def multilabel_probabilities(model, x):
    """Una probabilità per errore; non sono classi mutuamente esclusive."""
    result = []
    for classes, probabilities in zip(model[-1].classes_, model.predict_proba(x)):
        matches = np.flatnonzero(classes == 1)
        result.append(probabilities[:, matches[0]] if len(matches) else np.zeros(len(x)))
    return np.column_stack(result)

def predict(models, frame):
    """Restituisce final_risk, probabilità dei tipi e label singola/multipla predetta."""
    x = prepare_features(frame, models['feature_columns'])
    risk = positive_probability(models['risk_model'], x)
    augmented = x.copy()
    augmented['final_risk'] = risk
    probabilities = multilabel_probabilities(models['label_model'], augmented)
    labels = models['error_labels']
    bits = probabilities >= models['label_threshold']
    result = pd.DataFrame(index=frame.index)
    if 'timestamp' in frame:
        result['timestamp'] = frame.timestamp
    result['final_risk'] = risk
    for i, label in enumerate(labels):
        result[f'probability_{label}'] = probabilities[:, i]
    result['predicted_label'] = ['+'.join(label for label, active in zip(labels, row) if active)
                                 or 'normal' for row in bits]
    return result
