# IPTV
自用IPTV源

## 自动同步

播放列表继续使用原来的 `IPTV.m3u` 链接。在 `.github/iptv_sources.txt`
中每行添加一个已获授权使用的 M3U/M3U8 上游链接。

同步同时生成 `ywIPTV.m3u`，只保留央视和卫视频道；两份文件共用同一轮
FFmpeg 检测结果。

同步保留名称、分组、台标和备用线路，仅删除完全重复的条目。
电视源必须实际解码 3 秒视频，名称或分组含广播、电台、Radio、FM、AM
的条目允许解码音频。默认并发 8 路、每次 20 秒超时、失败重试一次。
不支持的播放指令或格式会阻止整批更新，不会静默丢弃。

上游失败、检测未完成、结果为空，或者通过数量不足上一版的一半，
都保留旧列表。首次无旧列表时至少要求输入条目的 20% 通过。
每次重新检测全部来源，不设置永久黑名单。

## 首次启用

1. 将这些文件发布到本仓库的 `main` 分支后，在 Actions 中手动运行
   **IPTV sync**，先不要勾选 Publish。
2. 确认运行报告、通过数量及失败原因；报告包含每个失败条目的序号，
   不公开完整播放地址或鉴权信息。
3. 手动运行并勾选 Publish，确认 `IPTV.m3u` 更新及原订阅链接可用。
4. 在仓库 Settings → Secrets and variables → Actions → Variables 中新增
   `IPTV_SYNC_ENABLED`，值为 `true`，开启每 6 小时的定时同步。
   删除该变量或改为 `false` 可暂停定时任务。

只有列表内容变化才自动提交。GitHub 云端的检测结果不保证家庭网络效果，
也不保证链接持续有效；若大量源在云端失败，不应降低安全阈值绕过检查。

## 本地检查

需要 Python 3.11+ 和 FFmpeg，无第三方 Python 依赖：

```sh
python -m unittest discover -s tests -v
python .github/workflows/iptv_sync.py --dry-run
```

Windows 子进程输出固定按 UTF-8 读取并替换无法解码的字节，避免 FFmpeg
错误信息按系统 GBK 解码时中断检测。
