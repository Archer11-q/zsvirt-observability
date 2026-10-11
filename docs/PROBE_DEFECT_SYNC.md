# 探针容器采集器缺陷同步单（给成员 A）

> **结论先说**：`agents/probe/collector/container.py` 的 `ContainerCollector`
> **永远采集不到任何容器事件**。不是偶尔失败、不是权限问题 —— 是四条独立缺陷叠加，
> 且**全部被静默吞掉**，所以单测全绿、日志无异常、上报也成功，只是事件数恒为 0。
>
> 影响面：`container.oom_killed` / `container.restart` 这两个事件类型在**真实采集
> 路径上是死数据**；B 侧 `R-CTR-OOM-010` / `R-CTR-RESTART-011` 永远不会由真实数据
> 触发；赛题三类场景里的**「容器/进程异常」在真实部署下无法演出来**。
>
> 发现方式：在开发机上用真实 Docker 容器跑「容器 OOM」场景时，
> 容器确实在反复 OOMKilled（`OOMKilled=true` / `ExitCode=137` / `RestartCount` 递增），
> 而探针上报的事件数始终为 0。逐层定位到下面四条。
>
> 仓库里**没有改动 `agents/` 的任何文件** —— 按项目纪律那是你的地盘。
> 我这边把修正放在桥接层（`deploy/sample-workload/collect_real_events.py`）以便先把
> 场景跑通，**但真实部署走的是 socket，桥接层不在链路上**，所以这四条必须由你修。

---

## 缺陷 1：`filters` 未做 URL 编码 → 请求根本没发出去

`DockerClient.events()`（`container.py` 约 60 行）：

```python
filters = {"type": ["container"], "event": ["oom", "die", "restart"]}
qs = f"/events?since={since}&until={until}&filters={json.dumps(filters)}"
```

`json.dumps` 的默认分隔符**带空格**（`": "` 与 `", "`），而 `http.client`
在发送前就拒绝含控制字符的 URL：

```
http.client.InvalidURL: URL can't contain control characters.
  '/events?since=...&filters={"type": ["container"], "event": ["oom", ...]}'
  (found at least ' ')
```

`InvalidURL` 继承 `HTTPException`，而 `_get` 的
`except (OSError, http.client.HTTPException, json.JSONDecodeError)` **正好把它吞掉**，
返回 `None` → `events()` 返回 `[]`。

**实测**（同一窗口、同一守护进程）：

| 查询构造方式 | 结果 |
|---|---|
| 当前写法（含空格） | 抛 `InvalidURL`，被吞，返回 0 条 |
| `urllib.parse.urlencode` 编码后 | HTTP 200，**23 条**事件（oom/die 交替） |

## 缺陷 2：`_get` 用 `json.loads` 解析**换行分隔的 JSON 流**

即使缺陷 1 修好，仍然拿不到事件。Docker `/events` 的响应体是 **NDJSON**：

```
{"Type":"container","Action":"oom","Actor":{...}}
{"Type":"container","Action":"die","Actor":{...}}
```

而 `_get` 结尾是 `return json.loads(body)` —— 对多行 NDJSON 必然抛
`JSONDecodeError`，又被同一个 `except` 吞掉，返回 `None`。

`/containers/json` 返回的是**真正的 JSON 数组**，所以资源那条路径没问题。
**同一个 `_get` 服务两种响应形状，而只实现了一种。**

⚠️ 这条特别值得注意：**只修缺陷 1 是无效的**。两条都修完 `events()` 才会返回数据 ——
这也是它难定位的原因，第一次修复看起来"没效果"。

## 缺陷 3：容器 ID 读错字段 → 事件挂不上任何资源

`ContainerCollector.collect()`（约 115 行）：

```python
cid = ev.get("id", "")
source_id = cid[:12]
```

Docker **Engine API** 的 `/events` 响应里**没有顶层 `id` 字段**，容器 ID 在
`Actor.ID`。实测：

```
ev.get('id')       = None
ev['Actor']['ID']  = 'ef193b87b224d12e45b8b85957b006fb87f3d7225aae6192dd0996cd987ab34b'
```

于是 `source_id = ""`，**每条事件的 `resource_source_id` 都是空串**。
这类事件比"没有事件"更难排查：事件条数看起来是对的，只是全都关联不到资源，
B 侧会把它归到占位节点或直接无法归因。

（注：`docker events` **命令行**的 `--format '{{json .}}'` 输出里是有 `id` 的，
所以拿 CLI 输出对照调试时不会暴露这一条 —— 这也是它容易漏掉的原因。）

## 缺陷 4：`restartCount` 读的字段不存在 → 恒为 0

```python
"restartCount": c.get("RestartCount", 0),
```

`c` 来自 `/containers/json?all=1`，而**该响应没有 `RestartCount` 字段**
（它在 `/containers/<id>/json`，即 inspect 里）。`.get(..., 0)` 于是永远拿默认值 0。

实测：容器已重启 14 次，上报的 `restartCount` 是 `0`。

这条与前三条的危害不同 —— 它不丢数据，而是**报一个看起来正常的错值**。
"重启 0 次"与"重启 14 次"在诊断里是完全不同的证据，而前者会让运维判断"这个容器没问题"。

---

## 建议的修法（一处集中修，避免再出"两种响应形状一个 `_get`"）

```python
import urllib.parse

class DockerClient:
    def _get(self, path: str) -> Any | None:
        """单个 JSON 文档（`/containers/json`、`/containers/<id>/json`）。"""
        body = self._request(path)
        if body is None:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def _get_stream(self, path: str) -> list[dict[str, Any]]:
        """NDJSON 流（`/events`）。**不要**用 `_get` —— 形状不同。"""
        body = self._request(path)
        if body is None:
            return []
        out = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue   # 单行坏掉不该丢掉整批
        return out

    def _request(self, path: str) -> str | None:
        conn = _UnixHTTPConnection(self._sock_path)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            return None if resp.status >= 400 else body
        except (OSError, http.client.HTTPException):
            return None
        finally:
            conn.close()

    def events(self, since: int, until: int) -> list[dict[str, Any]]:
        filters = {"type": ["container"], "event": ["oom", "die", "restart"]}
        query = urllib.parse.urlencode({
            "since": since,
            "until": until,
            # separators 收紧 + urlencode 编码，两个都要：
            # separators 去掉空格，urlencode 处理引号与方括号
            "filters": json.dumps(filters, separators=(",", ":")),
        })
        return self._get_stream(f"/events?{query}")

    def inspect(self, container_id: str) -> dict[str, Any] | None:
        data = self._get(f"/containers/{container_id}/json")
        return data if isinstance(data, dict) else None
```

`collect()` 里两处：

```python
# 缺陷 3：ID 在 Actor.ID，不在顶层 id
actor = ev.get("Actor") or {}
cid = actor.get("ID", "")

# 缺陷 4：restartCount 只存在于 inspect
```

## 请你补的回归测试（这是关键）

**这四条缺陷单测全都抓不到**，原因是测试用的是自造夹具 / 假 client ——
和之前 `resource_edge` 从不被填充、`_infer_parent_kind` 猜错类型那两次是同一个模式：
**测试自己造夹具，就抓不到"夹具与真实响应形状不一致"这类缺陷**。

建议至少加两条：

1. **NDJSON 解析**：给 `_get_stream` 喂一段**真实抓下来的** `/events` 响应体
   （两行以上），断言解析出 2 条。夹具请从真实 socket 抓，不要手写 ——
   手写就又把形状假设固化了。
2. **资源指向**：给 `collect()` 喂一条 `Actor.ID` 存在、顶层 `id` 缺失的真实事件，
   断言产出的 `Event.resource_source_id` **非空**且等于 `Actor.ID[:12]`。
   断言"非空"这一条最重要：空串不会报错，只会让事件全都挂不上。

另建议把 `/containers/json` 与 `/containers/<id>/json` 的真实响应各存一份作为夹具，
断言 `restartCount` 取自 inspect。

## 复现命令（在装有 Docker 的机器上）

```bash
# 1. 起一个必然 OOM 的容器
docker run -d --name oom-demo --memory=64m --memory-swap=64m \
  python:3.12-slim python -c "b=bytearray(200*1024*1024); b[::4096]=b'x'*len(b[::4096])"

# 2. 确认它真的被 OOM Killed
docker inspect oom-demo --format '{{.State.OOMKilled}} {{.State.ExitCode}}'

# 3. 用探针的 DockerClient 查事件 —— 当前会返回空
python3 -c "
import sys, time; sys.path.insert(0, 'agents')
from probe.collector.container import DockerClient
c = DockerClient(); now = int(time.time())
print('事件数 =', len(c.events(now - 300, now)))   # 期望 >0，当前恒为 0
"
```

---

## 附：桥接层现在怎么绕过去的

`deploy/sample-workload/collect_real_events.py` 里做了三件事，
**都只是为了在缺陷修好之前先把真实场景跑通**：

| 桥接层的动作 | 对应的缺陷 |
|---|---|
| `NativeDockerClient.events()` 自己发请求 + 逐行解析 NDJSON | 1 + 2 |
| `repair_event_targets()` 用 `Actor.ID[:12]` 补回资源指向 | 3 |
| `inspect_containers()` 补齐 `restartCount` 与指标 | 4 |

修好之后，桥接层的这三段都可以删掉 —— 它们的存在本身就是待办事项。

**实测结果**（修好前 vs 桥接层绕过后）：事件数 `0` → `container.oom_killed` 正常入库，
B 侧产出告警 `R-CTR-OOM-010`（firing）与根因 `CONTAINER_MEMORY_LIMIT`（置信 0.9）。
也就是说：**这四条修完，赛题三类场景里的「容器/进程异常」才能用真实数据演出来。**
