"""webui：面向浏览器的验收界面（Django 模板 + 原生 JS，无前端构建链）。

设计约束（用户要求）：

1. **继承原型风格**：配色/字号/边框/圆角都取 ``knowledge-base-prototype.html`` 的
   设计变量（灰白、1px 边框、2px 小圆角、13 号字），不另起一套视觉语言；
2. **session 登录**：用 Django 自带 auth + session，**浏览器里不保存 Basic 口令**；
3. 页面只是壳：数据全部走既有 REST API（``/api/v1/knowledge/...``），
   所以"界面能用"等价于"API 能用"，不存在两套逻辑；
4. 只读与写入口严格分开：上传/重试是写操作，来源与已入库内容一律只读展示。
"""

from __future__ import annotations

from django.apps import AppConfig


class WebuiConfig(AppConfig):
    name = "apps.webui"
    label = "webui"
    verbose_name = "浏览器验收界面"
    default_auto_field = "django.db.models.BigAutoField"
