# returnatlas 海鲜经营资料服务

这个 Python 项目为经营研究提供本地 CSV 资料、领域视图、数值剖面和不可变研究备注。现有经营资料的字段与计量语义见 `docs/domain.md`，HTTP 行为见 `docs/api.md`。服务不连接真实经营系统，也不自动执行交易或订货。

## 目录

`returnatlas/catalog.py` 解析并校验资料，`domain.py` 提供当前经营视图，`profile.py` 提供探索性数值摘要，`cluster.py` 实现分层 Poisson 空间/时空聚集扫描与地理不确定性稳定性分析，`reviews.py` 保存可回溯的不可变复核存档，`cases.py` 保存含资料副本的研究备注，`service.py` 处理 HTTP。`data/` 是可重复使用的虚构经营资料，`tests/` 检查当前接口和领域口径，`smoke.py` 发起真实本地 HTTP 请求后退出。聚集分析的完整统计口径见 `docs/cluster.md`。

## 本机运行

使用 Python 3.11，依赖版本固定在 `requirements.txt`。首次准备依赖后，运行和验证不访问公网。

```bash
python3 -m pip install -r requirements.txt
python3 -m returnatlas
```

默认资料目录为 `data`，研究备注写入 `state`；入口支持 `--data`、`--state`、`--host` 和 `--port`。研究记录含完整源表副本和内容校验，不会因源文件后续更改自动变成新的研究记录。

## 空间聚集复核线索

服务启动后可请求 `GET /clusters/scan`（参数见 `docs/api.md`，方法见 `docs/cluster.md`），也可以离线运行：

```bash
python3 -m returnatlas scan --mc 399 --realizations 100 --output report.json
python3 -m returnatlas scan --species 鱼 --week-from 2026-W35 --week-to 2026-W42 --spatial-only
```

扫描以完成配送订单为暴露、去重退货订单为事件，按品种×履约周分层标准化，用同一搜索网格上的置换蒙特卡洛控制族错误率；街道多候选点与服务边界版本通过重复实现和情景对比进入稳定性判断。没有配送的位置只列入无暴露清单，不解释为零风险；结论只支持街道/路段级，不产出住户标签。显著线索可通过 `POST /cluster-reviews` 连同引用证据固化为不可变存档。

## 测试和编译

在项目根目录执行，测试会自行创建临时状态目录并清理，HTTP 测试由系统分配空闲端口。

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q returnatlas tests smoke.py
python3 -m build --wheel --no-isolation
python3 smoke.py
```

## 容器运行

镜像固定 Python 基础镜像内容摘要及数值依赖版本，环境准备阶段需要包镜像。验证阶段可完全关闭网络，仅在容器内访问环回 HTTP。

```bash
docker build -t returnatlas:local .
docker run --rm --network none returnatlas:local python -m unittest discover -s tests -v
docker run --rm --network none returnatlas:local python smoke.py
docker run --rm -p 127.0.0.1:8080:8080 returnatlas:local
```

完整接口检查通常只需数秒。数值剖面的金额转为浮点仅为探索用途，成本金额对账仍以原始十进制字段为准。
