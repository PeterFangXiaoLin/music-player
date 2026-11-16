# B 面 · 本地音乐播放器

粘贴 B 站或 YouTube 视频链接，播放器会提取可直接播放的音轨、保存到本地，并自动加入歌单。支持顺序播放、随机播放、单曲循环、自动切歌、进度拖动、音量调整和删除歌曲。

## Windows 启动

安装 Python 3.11 或以上版本，双击 `start.bat`。首次启动会创建 `.venv` 并联网安装依赖，完成后打开 [本地播放器](http://127.0.0.1:8765)。播放时保持启动窗口开启，按 `Ctrl+C` 停止服务。

端口被占用时，可以在终端执行 `.\start.bat --port 8766`。也可以手动启动。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py --open
```

## 使用

1. 复制一个你有权保存的视频链接。B 站支持 `/video/BV…`、`/video/av…` 和 `b23.tv`，分 P 可用 `?p=2` 指定。YouTube 支持 `youtube.com/watch?v=…`、`youtu.be/…` 和 Shorts。带播放列表参数的链接只导入当前视频。
2. 粘贴链接并点击「导入音频」，完成后点击歌单里的歌曲或底部播放按钮。
3. 选择「顺序」按歌单排列播放，或选择「随机」打乱其余曲目。这两种模式每轮播完会停止。选择「单曲循环」会重复播放当前歌曲，手动切歌后循环新选中的歌曲。
4. 点击歌曲右侧的「↑」或「↓」调整位置。新顺序会自动保存，并用于顺序播放。

歌单保存在 `data/library.sqlite3`，音频保存在 `data/audio/`。已导入的音频支持离线播放，封面依赖网络。删除歌曲会同时删除本地音频。备份时先停止程序，再复制 `library.sqlite3` 和 `audio/`。

导入需要独立音轨。目前支持 AAC/M4A 和 WebM，无需 FFmpeg。YouTube 导入需要本机能访问 YouTube，并在 `PATH` 中有 [Node.js 22+ 或 Deno 2.3+](https://github.com/yt-dlp/yt-dlp/wiki/EJS)。直播请等回放处理完毕后再导入。

### YouTube 要求登录验证

1. 在浏览器登录 YouTube，完成网站提示的验证，并确认同一视频在该账号下能播放。
2. 按 yt-dlp 的说明导出 `youtube.com` 的 Netscape 格式 Cookie，保存为项目里的 `data/youtube-cookies.txt`。[YouTube Cookie 导出说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)
3. 回到播放器用同一个链接重新导入。程序会在每次 YouTube 导入时读取该文件。若登录失效，重新导出并替换文件。删除文件后恢复匿名导入。

Cookie 文件包含账号登录凭证，请留在本机，避免分享、提交到 Git 或随歌单备份。如果 YouTube 仍返回 HTTP 429，请先等请求限制解除再重试。

若之前能导入的视频突然失败，可以检查视频和网络状态，并尝试更新解析器。

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade "yt-dlp[default]"
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```



### 效果图

![image-20261113081612237](https://raw.githubusercontent.com/PeterFangXiaoLin/imgs/main/test/20261113081621118.png)
