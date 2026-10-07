# IPTV

GitHub Actions 自动同步的 M3U 播放列表。

## 订阅文件

- `IPTV.m3u`：全部通过安全检查的频道。
- `ywIPTV.m3u`：仅央视和卫视，排除央视外语、游戏、足球、熊猫直播。

原有 `IPTV.m3u` 订阅地址不变。`ywIPTV.m3u` 是额外的央卫列表。

## 自动同步

来源按 `.github/iptv_sources.txt` 的顺序合并，当前主源为
`best-fan/iptv-sources`，随后补充 Collect-IPTV 和 mzky。

每天北京时间约 **06:07、12:07、16:07、20:07** 运行。GitHub 的定时任务
可能延迟，不能保证精确到分钟。仓库变量 `IPTV_SYNC_ENABLED=true` 才会启用
定时任务；也可以在 Actions 中手动运行。

检测流程为：

1. 对每条 HTTP/HTTPS 线路做约 4 秒快速探测，过滤空响应、HTML 广告页和明显
   的网络错误。
2. 对通过探测的候选线路用 FFmpeg 实际解码 3 秒视频；广播线路允许只解码音频。
3. 默认 16 路并发，完整检测超时 20 秒，失败重试一次。

快速探测只是预筛选，不代表线路一定能播放；最终结果以 FFmpeg 解码为准。

## 安全规则

- 上游下载失败、格式异常、检测中断或有效线路不足时，不覆盖旧的全量列表。
- 央卫列表保证尽量保留 CCTV-1 至 CCTV-15：优先当前通过线路，其次使用旧列表
  或当前候选；候选未通过完整解码时会标记 `unverified fallback`。
- `.github/iptv_blocked_urls.txt` 只精确暂时封禁已确认的广告线路，不会封禁同一
  服务器的其他地址。
- Actions artifact 中的 `host-review.m3u` 列出两个相关服务器的全部线路、上游
  来源和当前封禁状态，仅供排查，不是订阅文件；URL 查询参数值会打码。

频道名生成时会移除末尾的 `(720p)`、`(1080p)`、`(576i)`、`(HD)`、`(SD)`。
原名称会以 `# Original-Name` 注释保留。dry-run 只生成报告，不写入播放列表；
需要实际发布时才会更新列表。

GitHub 云端检测只代表运行当次的云端网络环境，不保证家庭网络效果或长期可用。

## 本地检查

需要 Python 3.11+ 和 FFmpeg，无第三方 Python 依赖：

```sh
python -m unittest discover -s tests -v
python .github/workflows/iptv_sync.py --dry-run
```

Windows 下 FFmpeg 输出按 UTF-8 读取并替换异常字节，避免本地编码差异中断检测。
