"""Ограниченный HTTP-бенчмарк авторизованного чтения без вызовов модели."""

import argparse
import asyncio
import json
import platform
import time
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
READ_PATHS = {'documents': '/api/v1/documents?limit=20', 'packages': '/api/v1/packages?limit=20'}


def percentile(values: list[float], fraction: float) -> float:
    """Получить наблюдаемую процентиль без интерполяции малой выборки."""
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))]


def summarize(samples: list[tuple[int, float]], elapsed: float) -> dict:
    """Подсчитать фактические коды, throughput и задержки успешных ответов."""
    codes = {}
    for code, _ in samples:
        codes[str(code)] = codes.get(str(code), 0) + 1
    good = [duration for code, duration in samples if code == 200]
    return {
        'requests': len(samples), 'status_counts': codes,
        'elapsed_seconds': round(elapsed, 3),
        'rps': round(len(samples) / elapsed, 2) if elapsed else 0,
        'latency_ms': {
            'p50': round(percentile(good, .50), 2),
            'p95': round(percentile(good, .95), 2),
            'max': round(max(good), 2),
        } if good else None,
    }


async def read_benchmark(client: httpx.AsyncClient, path: str, duration: float, concurrency: int) -> dict:
    """Нагружать только авторизованный GET до общего времени остановки."""
    for _ in range(3):
        response = await client.get(path)
        response.raise_for_status()
    samples = []
    start = time.perf_counter()
    deadline = start + duration

    async def worker():
        while time.perf_counter() < deadline:
            begin = time.perf_counter()
            try:
                response = await client.get(path)
                code = response.status_code
            except httpx.HTTPError:
                code = 0
            samples.append((code, (time.perf_counter() - begin) * 1000))

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    result = summarize(samples, time.perf_counter() - start)
    result['concurrency'] = concurrency
    result['target_duration_seconds'] = duration
    return result


async def run(args):
    """Запустить измерения с одним HTTP-клиентом и сохранить обезличенный JSON."""
    token = (ROOT / 'secrets/accountant_token').read_text().strip()
    if not token:
        raise SystemExit('Missing accountant_token secret')
    result = {
        'platform': platform.platform(), 'machine': platform.machine(),
        'client': 'На том же компьютере, где запущен Docker Desktop', 'base_url': args.base_url,
        'workload': 'Авторизованное чтение. Во время замера загрузка документов и OCR не выполнялись',
        'reads': {},
    }
    limits = httpx.Limits(max_connections=max(args.concurrency, 4), max_keepalive_connections=max(args.concurrency, 4))
    async with httpx.AsyncClient(base_url=args.base_url, headers={'Authorization': 'Bearer ' + token},
                                 timeout=30, limits=limits) as client:
        for name, path in READ_PATHS.items():
            result['reads'][name] = await read_benchmark(client, path, args.duration, args.concurrency)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(f"Saved measured results to {args.output}")
    for name, row in result['reads'].items():
        print(f"{name}: {row['requests']} requests, {row['rps']} RPS, p95={row['latency_ms']['p95'] if row['latency_ms'] else 'n/a'} ms, codes={row['status_counts']}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--duration', type=float, default=15)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--output', type=Path, default=ROOT.parent / 'docs/benchmarks/local-load.json')
    options = parser.parse_args()
    if not 1 <= options.concurrency <= 16 or not 1 <= options.duration <= 120:
        parser.error('Use concurrency 1–16 and duration 1–120 s')
    asyncio.run(run(options))
