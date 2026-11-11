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
    "offline": ("等待手机蓝牙连接", "打开「智库随行」配对连接", "待发送录音仅在内存保留"),
    "error": ("本轮处理未完成", "请到网页查看具体状态", "重新长按可发起下一轮"),
    "setup": ("等待设备配置", "请通过电脑配置", "服务地址与设备凭据"),
    "canceled": ("已取消发送", "这条录音不会提交给 AI", "按住按钮可重新说话"),
    "canceling": ("正在取消发送", "请保持手机蓝牙连接", "确认前不会重发这条录音"),
    "cancel_late": ("来不及取消", "这条录音已进入 AI 处理", "请等待结果或到网页查看"),
    "hotspot": ("没有找到手机热点", "请在手机热点配置中", "选择 2.4 GHz 或兼容模式"),
    "clock": ("热点已连接", "正在校准网络时间", "校准后连接知识库"),
    "server": ("手机已连接", "正在连接知识库", "检查电脑与远程入口是否在线"),
    "pairing": ("蓝牙安全配对", "请在手机输入下方配对码", ""),
    "standby": ("日历 · 等待手机校时", "暂无任务信息（离线）", "按键唤醒 · 按住说话"),
    "submitted": ("任务已接收", "正在后台处理", "可以继续按住说下一条"),
    "task_full": ("四个任务正在处理", "录音正在等待发送", "可双击取消这条等待录音"),
    "task_selected": ("", "按住正面按钮说话", "长按下键可切换任务"),
    "menu_wait": ("正在切换任务", "正在等待手机确认", "请稍候，超时后可重试"),
    "menu_offline": ("手机蓝牙未连接", "请打开智库随行连接设备", "任务未切换 · 可返回重试"),
    "menu_error": ("未能确认任务切换", "请检查手机和网络后重试", "可返回菜单重新选择"),
    "upload_error": ("这条录音尚未送达", "知识库没有收到本条录音", "检查手机蓝牙后重新录音"),
    "upload_rejected": ("知识库未接收录音", "请在手机查看连接状态", "重新选择任务后再录音"),
    "restarting": ("正在重启", "配对和任务记录会保留", "请稍候，手机会自动重连"),
    "restart_busy": ("请先完成当前发送", "录音或任务选择尚未完成", "完成后长按上键 5 秒重启"),
}
for name, lines in states.items():
    image = Image.new("1", (400, 300), 1)
    draw = ImageDraw.Draw(image)
    draw.text((16, 10), "智库 · Note 4", font=font, fill=0)
    draw.line((12, 48, 388, 48), fill=0)
    if name == "task_selected":
        draw.text((16, 54), "当前任务", font=font.font_variant(size=15), fill=0)
    for i, line in enumerate(lines):
        draw.text((16, 80 + i * 58), line, font=font, fill=0)
    (root / f"{name}.bin").write_bytes(image.tobytes())
print("Generated", len(states), "state screens")
