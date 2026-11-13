# B 面 · 本地音乐播放器

粘贴 B 站视频链接，播放器会提取可直接播放的音轨、保存到本地，并自动加入歌单。支持顺序播放、随机播放、自动切歌、进度拖动、音量调整和删除歌曲。

## Windows 启动

安装 Python 3.11 或以上版本，双击 `start.bat`。首次启动会创建 `.venv` 并联网安装依赖，完成后打开 [本地播放器](http://127.0.0.1:8765)。播放时保持启动窗口开启，按 `Ctrl+C` 停止服务。

端口被占用时，可以在终端执行 `.\start.bat --port 8766`。也可以手动启动。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py --open
```

## 使用

1. 从 B 站复制一个你有权保存的视频链接。支持 `/video/BV…`、`/video/av…` 和 `b23.tv` 短链接。分 P 视频可用链接里的 `?p=2` 指定第 2 P，省略时导入第 1 P。
2. 粘贴链接并点击「导入音频」，完成后点击歌单里的歌曲或底部播放按钮。
3. 选择「顺序」按歌单排列播放，或选择「随机」打乱其余曲目。每轮播完会停止，选择另一首歌可开始新一轮。

歌单保存在 `data/library.sqlite3`，音频保存在 `data/audio/`。已导入的音频支持离线播放，封面依赖网络。删除歌曲会同时删除本地音频。备份时先停止程序，再复制整个 `data/` 目录。

导入依赖 B 站可公开播放的独立音轨。目前支持 AAC/M4A 和 WebM，无需 FFmpeg。若之前能导入的视频突然失败，可以检查视频和网络状态，并尝试更新解析器。

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade yt-dlp
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```



### 效果图

![image-20261113081612237](https://raw.githubusercontent.com/PeterFangXiaoLin/imgs/main/test/20261113081621118.png)
