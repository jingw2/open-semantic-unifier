"""Synthetic demo data used only by the legacy walkthrough flows."""

MASTER_SUPPLIERS = [
    {"id": "SUP-1001", "name": "星云智造科技有限公司", "aliases": ["星云智造", "星云制造科技", "星云智造(深圳)有限公司", "星云智造（深圳）有限公司", "  XingYun  ZhiZao Tech!  ", "tech xingyun zhizao"]},
    {"id": "SUP-1002", "name": "苍穹精密模具厂", "aliases": ["苍穹模具", "苍穹精密", "仓穹精密模具厂", "东莞苍穹精密模具厂", "CangQiong Mold Co., Ltd."]},
    {"id": "SUP-1003", "name": "极光跨境物流集团", "aliases": ["极光物流", "极光跨境", "极光国际物流集团", "极光垮境物流", "Aurora Cross-Border Logistics"]},
    {"id": "SUP-1004", "name": "瀚海半导体材料有限公司", "aliases": ["瀚海半导体", "瀚海材料", "上海瀚海半导体材料有限公司", "Hanhai Semi Material"]},
    {"id": "SUP-1005", "name": "青鸾智能装备股份有限公司", "aliases": ["青鸾智能", "青鸾装备", "苏州青鸾智能装备", "QingLuan Intelligent Equip"]},
    {"id": "SUP-1006", "name": "玄武工业自动化技术有限公司", "aliases": ["玄武自动化", "玄武工业", "武汉玄武工业自动化", "XuanWu Auto Tech"]},
    {"id": "SUP-1007", "name": "赤霄新能源电池科技有限公司", "aliases": ["赤霄新能源", "赤霄电池", "宁德赤霄新能源电池", "ChiXiao Energy Tech"]},
    {"id": "SUP-1008", "name": "昆仑精密光学仪器有限公司", "aliases": ["昆仑光学", "昆仑精密光学", "成都昆仑光学仪器", "KunLun Optics Co."]},
    {"id": "SUP-1009", "name": "凌云航空复合材料有限公司", "aliases": ["凌云复材", "凌云航空材料", "西安凌云航空复合材料", "LingYun Aero Composites"]},
    {"id": "SUP-1010", "name": "沧澜冷链供应链管理有限公司", "aliases": ["沧澜冷链", "沧澜供应链", "广州沧澜冷链物流", "CangLan Cold Chain"]},
    {"id": "SUP-1011", "name": "破军特种钢材制造有限公司", "aliases": ["破军特钢", "破军钢材", "鞍山破军特种钢材", "PoJun Special Steel"]},
    {"id": "SUP-1012", "name": "天枢云计算数据服务有限公司", "aliases": ["天枢云", "天枢数据", "杭州天枢云计算", "TianShu Cloud Data"]},
    {"id": "SUP-1013", "name": "瑶光生物医药包装有限公司", "aliases": ["瑶光包装", "瑶光医药包装", "泰州瑶光生物医药包装", "YaoGuang BioPack"]},
    {"id": "SUP-1014", "name": "北斗高精度传感器科技有限公司", "aliases": ["北斗传感器", "北斗传感科技", "北京北斗高精度传感器", "BeiDou Sensor Tech"]},
    {"id": "SUP-1015", "name": "麒麟环保新材料股份有限公司", "aliases": ["麒麟环保", "麒麟新材料", "合肥麒麟环保新材料", "QiLin Eco Materials"]},
]


def supplier_catalog_for_sources(sources):
    """Return demo identities only when a source was explicitly loaded from the demo route."""
    if any((source.routing_json or {}).get("demo_catalog_key") == "supplier_master_demo_v1" for source in sources):
        return MASTER_SUPPLIERS
    return None
