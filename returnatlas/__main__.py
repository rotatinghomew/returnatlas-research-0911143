import argparse
import json
from pathlib import Path

from .cluster import (BOUNDARY_SCENARIOS, build_boundary_versions, build_frame,
                      build_geometry, cluster_scan)
from .service import Application, make_server


def run_scan_command(arguments):
    from .catalog import Catalog

    catalog = Catalog(arguments.data)
    species = arguments.species.split(",") if arguments.species else None
    frame = build_frame(catalog, species=species,
                        week_from=arguments.week_from, week_to=arguments.week_to)
    geometry = build_geometry(catalog)
    versions = build_boundary_versions(catalog)
    report = cluster_scan(
        catalog, geometry, frame, versions, scenario=arguments.scenario,
        replications=arguments.mc, realization_count=arguments.realizations,
        min_events=arguments.min_events, include_time=not arguments.spatial_only,
        seed=arguments.seed)
    payload = {"source_fingerprint": catalog.fingerprint, "report": report}
    output = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
    if arguments.output:
        arguments.output.write_text(output, encoding="utf-8")
    else:
        print(output)


def _add_scan_arguments(parser):
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--species", help="品种白名单，逗号分隔；省略表示全部品种")
    parser.add_argument("--week-from", dest="week_from", help="起始履约周，例如 2026-W31")
    parser.add_argument("--week-to", dest="week_to", help="截止履约周，例如 2026-W43")
    parser.add_argument("--scenario", choices=BOUNDARY_SCENARIOS, default="time_varying")
    parser.add_argument("--mc", type=int, default=399, help="蒙特卡洛重复次数")
    parser.add_argument("--realizations", type=int, default=100, help="定位不确定性实现次数")
    parser.add_argument("--min-events", dest="min_events", type=int, default=2)
    parser.add_argument("--spatial-only", action="store_true", help="只扫描空间窗，不扫描时间前缀")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", type=Path, help="报告写入路径；省略则打印到标准输出")
    parser.set_defaults(func=run_scan_command)


def main():
    parser = argparse.ArgumentParser(description="经营研究资料服务")
    # 无子命令时保持历史调用形式：python -m returnatlas --host ...
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动本机 HTTP 服务（默认行为）")
    serve.add_argument("--data", type=Path, default=Path("data"))
    serve.add_argument("--state", type=Path, default=Path("state"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)

    scan = subparsers.add_parser("scan", help="执行空间聚集扫描并输出复核线索")
    _add_scan_arguments(scan)

    arguments = parser.parse_args()
    if arguments.command == "scan":
        arguments.func(arguments)
        return

    server = make_server(Application(arguments.data, arguments.state),
                         arguments.host, arguments.port)
    print(f"资料服务已启动：{arguments.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
