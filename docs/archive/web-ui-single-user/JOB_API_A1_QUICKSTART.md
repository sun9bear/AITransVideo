# Job API A1 Quickstart

## 启动

```bash
python main.py job-api
```

默认地址：
- `http://127.0.0.1:8877`

## 创建任务

```bash
curl -X POST http://127.0.0.1:8877/jobs ^
  -H "Content-Type: application/json" ^
  -d "{\"job_type\":\"localize_video\",\"source\":{\"type\":\"youtube_url\",\"value\":\"https://www.youtube.com/watch?v=demo\"},\"output_target\":\"editor\"}"
```

## 查询状态

```bash
curl http://127.0.0.1:8877/jobs/<job_id>
```

## 查询日志

```bash
curl http://127.0.0.1:8877/jobs/<job_id>/logs
```

## review 后继续

先在 authoritative `review_state.json` 中完成对应 stage 的审批，再调用：

```bash
curl -X POST http://127.0.0.1:8877/jobs/<job_id>/continue
```

## A1 边界

- A1 当前只正式支持 `source.type=youtube_url`
- A1 当前只正式支持 `output_target=editor`
- A1 当前是单活跃任务语义
- A1 是本地最小集成后端，不是完整 Web / Skill，也不是生产级公网服务
- A1 的 stdout 路径推断当前按 Windows 本地运行环境校验
