"""Prepara dataset_processed e salva il preprocessore addestrato sul train."""
import argparse
import json
from pathlib import Path
import pandas as pd
from shared.preprocessing import RiskProcessor

def trajectory_csvs(folder):
    return [p for p in sorted(folder.rglob('*.csv'))
            if p.name != 'geometry.csv' and not p.name.endswith(('.geometry.csv','.truth.csv'))]


def geometry_for(path):
    common = path.parent/'geometry.csv'
    individual = path.with_suffix('.geometry.csv')
    if common.exists():
        return common
    if individual.exists():
        return individual
    raise ValueError(f'{path}: manca geometry.csv o {individual.name}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, default=Path('dataset'))
    parser.add_argument('--output-dir', type=Path, default=Path('dataset_processed'))
    parser.add_argument('--config', type=Path, help='JSON opzionale: cfg, speed_to_ms')
    parser.add_argument('--model-path', type=Path, default=Path('models/preprocessing.joblib'))
    args = parser.parse_args(argv)
    source, destination = args.input_dir.resolve(), args.output_dir.resolve()
    # Impedisce di sovrascrivere l'input o rileggere output annidati come input.
    if source == destination or source in destination.parents or destination in source.parents:
        parser.error('Input e output devono essere cartelle separate, non annidate')
    splits = ('train','validation','test')
    files = {}
    for split in splits:
        folder = source/split
        if not folder.is_dir():
            parser.error(f'Cartella mancante: {folder}')
        files[split] = trajectory_csvs(folder)
        if not files[split]:
            parser.error(f'Nessun CSV temporale in {folder}')
    # Verifica le geometrie prima di creare la cartella di output.
    paths = [p for split in splits for p in files[split]]
    geometries = {p: geometry_for(p) for p in paths}
    config = json.loads(args.config.read_text()) if args.config else {}
    unknown = set(config)-{'cfg','speed_to_ms'}
    if unknown:
        parser.error(f'Chiavi configurazione sconosciute: {sorted(unknown)}')
    processor = RiskProcessor(**config).fit(files['train'])
    processor.save(args.model_path)
    count = 0
    for split in splits:
        for path in files[split]:
            result = processor.transform(pd.read_csv(path), pd.read_csv(geometries[path]))
            target = destination/path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            result.to_csv(target, index=False)
            count += 1
        print(f'{split}: {len(files[split])} CSV elaborati')
    print(f'{count} CSV salvati in {destination}')


if __name__ == '__main__':
    main()
