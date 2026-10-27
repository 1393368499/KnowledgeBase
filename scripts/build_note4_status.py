"""Generate local state screens; no device secrets are embedded in firmware."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parent.parent / "firmware/note4/main/assets"
root.mkdir(parents=True, exist_ok=True)
font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 23)
states = {
    "ready": ("准备好了", "按住正面按钮说话", "双击取消发送 · 上下翻页"),
    "recording": ("正在录音", "按住说话，松手发送", "按上键取消 · 最长 60 秒"),
    "uploading": ("正在发送录音", "正在连接本地知识库", "双击正面按钮可申请取消"),
    "transcribing": ("正在识别", "录音已接收", "识别后自动发送到会话"),
    "thinking": ("正在回答", "文字已发送到知识库", "你说的内容可用短按查看"),
    "offline": ("连接暂不可用", "检查手机热点和电脑服务", "待发送录音仅在内存保留"),
    "error": ("本轮处理未完成", "请到网页查看具体状态", "重新长按可发起下一轮"),
    "setup": ("等待设备配置", "请通过电脑配置 Wi-Fi", "以及服务地址、设备凭据"),
    "canceled": ("已取消发送", "这条录音不会提交给 AI", "按住按钮可重新说话"),
    "canceling": ("正在取消发送", "请保持手机热点连接", "确认前不会重发这条录音"),
    "cancel_late": ("来不及取消", "这条录音已进入 AI 处理", "请等待结果或到网页查看"),
    "hotspot": ("没有找到手机热点", "请在手机热点配置中", "选择 2.4 GHz 或兼容模式"),
    "clock": ("热点已连接", "正在校准网络时间", "校准后连接知识库"),
    "server": ("热点已连接", "正在连接知识库", "检查电脑与远程入口是否在线"),
}
for name, lines in states.items():
    image = Image.new("1", (400, 300), 1)
    draw = ImageDraw.Draw(image)
    draw.text((16, 10), "智库 · Note 4", font=font, fill=0)
    draw.line((12, 48, 388, 48), fill=0)
    for i, line in enumerate(lines):
        draw.text((16, 80 + i * 58), line, font=font, fill=0)
    (root / f"{name}.bin").write_bytes(image.tobytes())
print("Generated", len(states), "state screens")
