#!/usr/bin/env python3
"""
Скачиватель MCD64A1 (MODIS Burned Area, коллекция 6.1) с NASA.

Standalone-скрипт для запуска с хоста (вне Docker): находит месячные
синусоидальные тайлы через CMR (поисковая система NASA Earthdata),
скачивает их в fire-monitor/data/mcd64a1/, который шарится с контейнерами
через volume.

Важно: данные теперь раздаются LP DAAC (data.lpdaac.earthdatacloud.nasa.gov)
по прямым ссылкам из CMR — старые ladsweb allData-URLы устарели.

Использование:
    python downnasagov.py --bbox 97,51,119,60 --year 2024 --month 7
    python downnasagov.py --bbox 97,51,119,60 --period 2024-07-01,2024-07-31

Токен: EARTHDATA_TOKEN — из переменной окружения или fire-monitor/.env
(https://urs.earthdata.nasa.gov -> профиль -> Generate Token).
Зависимости: только requests (pip install requests).
"""
import argparse
import calendar
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit('Нужна библиотека requests: pip install requests')

CMR_URL = 'https://cmr.earthdata.nasa.gov/search/granules.json'
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / 'data' / 'mcd64a1'
ENV_FILE = HERE.parent / '.env'

# Шаг сетки точек поиска (тайлы MODIS ~10-17° в долготе, 10° в широте)
LON_STEP = 8.0
LAT_STEP = 5.0
MARGIN = 3.0
CMR_PAUSE_SEC = 1.5  # лимит CMR: 20 запросов/мин


def load_token():
    token = os.environ.get('EARTHDATA_TOKEN', '').strip()
    if token:
        return token
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('EARTHDATA_TOKEN='):
                token = line.split('=', 1)[1].strip()
                if token:
                    return token
    sys.exit('EARTHDATA_TOKEN не найден: задайте переменную окружения '
             'или добавьте в fire-monitor/.env')


def month_temporal(year, month):
    last = calendar.monthrange(year, month)[1]
    return f'{year}-{month:02d}-01T00:00:00Z,{year}-{month:02d}-{last}T23:59:59Z'


def find_granules(bbox, year, month):
    """
    Найти гранулы MCD64A1 за месяц, покрывающие bbox, точечными CMR-запросами
    по сетке точек над bbox (расширенным на MARGIN градусов).

    Возвращает {granule_title: direct_url}.
    """
    lon0, lat0, lon1, lat1 = (float(x) for x in bbox)
    lon0 -= MARGIN
    lon1 += MARGIN
    lat0 = max(-90.0, lat0 - MARGIN)
    lat1 = min(90.0, lat1 + MARGIN)

    granules = {}
    session = requests.Session()
    lons = [lon0 + i * LON_STEP for i in range(int((lon1 - lon0) // LON_STEP) + 1)]
    lats = [lat0 + i * LAT_STEP for i in range(int((lat1 - lat0) // LAT_STEP) + 1)]
    if lons[-1] < lon1:
        lons.append(lon1)
    if lats[-1] < lat1:
        lats.append(lat1)

    for lon in lons:
        for lat in lats:
            params = {
                'short_name': 'MCD64A1',
                'point': f'{lon:.4f},{lat:.4f}',
                'temporal': month_temporal(year, month),
                'page_size': 1,
            }
            try:
                r = session.get(CMR_URL, params=params, timeout=60)
                if r.status_code != 200:
                    print(f'  CMR HTTP {r.status_code} для точки ({lon:.1f}, {lat:.1f})')
                    continue
                entries = r.json().get('feed', {}).get('entry', [])
            except (requests.RequestException, ValueError) as e:
                print(f'  CMR ошибка для точки ({lon:.1f}, {lat:.1f}): {e}')
                continue
            if entries:
                for link in entries[0].get('links', []):
                    href = link.get('href', '')
                    if link.get('rel', '').endswith('data#') and href.endswith('.hdf'):
                        granules[entries[0]['title']] = href
                        break
            time.sleep(CMR_PAUSE_SEC)
    return granules


def download(session, title, url, out_dir):
    cached = out_dir / f'{title}.hdf'
    if cached.exists():
        print(f'  [кэш] {title}.hdf')
        return cached
    print(f'  скачиваю {title} ...')
    r = session.get(url, stream=True, timeout=(30, 1800))
    if r.status_code in (401, 403):
        sys.exit(f'HTTP {r.status_code}: токен истёк или не приняты Terms of Use. '
                 'Пересоздайте токен на https://urs.earthdata.nasa.gov')
    r.raise_for_status()
    tmp = out_dir / f'.{title}.hdf.part'
    with open(tmp, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    tmp.rename(cached)
    print(f'  готово: {title}.hdf ({cached.stat().st_size / 1e6:.1f} МБ)')
    return cached


def main():
    global OUT_DIR
    ap = argparse.ArgumentParser(
        description='Скачивание тайлов MCD64A1 с NASA (через CMR + LP DAAC)')
    ap.add_argument('--bbox', required=True,
                    help='minx,miny,maxx,maxy (WGS84), напр. 97,51,119,60')
    ap.add_argument('--year', type=int)
    ap.add_argument('--month', type=int)
    ap.add_argument('--period', help='YYYY-MM-DD,YYYY-MM-DD (все месяцы периода)')
    ap.add_argument('--out', default=str(OUT_DIR),
                    help='папка вывода (по умолчанию data/mcd64a1)')
    args = ap.parse_args()

    OUT_DIR = Path(args.out)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    bbox = [float(x) for x in args.bbox.split(',')]
    if len(bbox) != 4:
        ap.error('--bbox: нужно 4 числа minx,miny,maxx,maxy')

    if args.period:
        d1, d2 = (datetime.strptime(s.strip(), '%Y-%m-%d')
                  for s in args.period.split(','))
        months = []
        y, m = d1.year, d1.month
        while (y, m) <= (d2.year, d2.month):
            months.append((y, m))
            m += 1
            if m == 13:
                y, m = y + 1, 1
    elif args.year and args.month:
        months = [(args.year, args.month)]
    else:
        ap.error('укажите --period YYYY-MM-DD,YYYY-MM-DD или --year/--month')

    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {load_token()}'})

    ok = 0
    for (y, m) in months:
        print(f'{y}-{m:02d}: поиск гранул в CMR ...')
        granules = find_granules(bbox, y, m)
        if not granules:
            print('  гранул не найдено — проверьте bbox и месяц')
            continue
        print(f'{y}-{m:02d}: найдено {len(granules)} тайлов')
        for title, url in granules.items():
            download(session, title, url, OUT_DIR)
            ok += 1
    print(f'\nГотово: скачано {ok} тайлов → {OUT_DIR}')


if __name__ == '__main__':
    main()
