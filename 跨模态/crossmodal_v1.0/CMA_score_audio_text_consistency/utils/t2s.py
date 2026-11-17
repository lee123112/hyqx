"""繁体→简体字符级归一化（离线自包含，零新增依赖）。

问题背景
--------
上游 Whisper-base 的中文转写偶尔输出繁体字（例如「中心六區中間促成…」），而 #9 的
对齐描述是简体（「中心六区中介促成…」）。若直接做字符级编辑距离，「繁体/简体书写
差异」会被误算成语义不一致；第八轮真实数据（crossmodal_test3）中因此有大量记录被
隔离。本模块只做逐字符的繁→简映射，把两侧文本先投影到同一书写体系，再交给现有
CER/编辑距离口径比较。

严格边界
--------
- 只做单字符映射，取 OpenCC ``TSCharacters`` 每条记录的首个简体候选；不做词组消歧，
  不改写英文、数字、标点，也不改变 ``edit_distance_ratio_max`` 的语义；
- 运行时**绝不联网**：映射表解析顺序为「算子包自带 → ``$MODEL_ROOT/opencc`` →
  代码内置高频子集」；
- 词典文件损坏/缺失会被安全跳过并继续尝试下一来源，不会让评分流程因词典问题崩溃
  （只是归一化收益变小），来源与条目数可通过 :func:`mapping_snapshot` 审计。

映射表来源（OpenCC，Apache-2.0）
-------------------------------
``utils/data/TSCharacters.txt`` 来自
https://github.com/BYVoid/OpenCC/blob/master/data/dictionary/TSCharacters.txt ，
文件头自带 OpenCC 的 Apache-2.0 声明；SHA256 与下载日期记录在
``utils/data/README.md``。格式为 ``繁体<TAB>简体候选1 简体候选2 ...``，``#`` 开头为注释。

已知边界
--------
单字级映射无法消歧繁简同形异义字（例如「乾」在简体中读 qián 的「乾坤/乾隆」、
「於」作姓氏/文言用法等），此时按 OpenCC 首候选转换，属本算子「只做字符级」口径的
已知取舍：真实第八轮 82 条记录的简体描述侧 0 个字符被改动；需要词组级精度时应在
上游做 OpenCC 词组转换，本算子保持字符级、且可用 ``traditional_to_simplified`` 关闭。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 未显式配置时的默认开关；config 的 ``traditional_to_simplified`` 默认值与之一致。
DEFAULT_ENABLED = True

_TABLE_FILENAME = "TSCharacters.txt"
#: 低于该条目数的文件视为损坏/非 TSCharacters 格式，继续尝试下一来源。
_MIN_TABLE_ENTRIES = 100

SOURCE_PACKAGE = "package"
SOURCE_MODEL_ROOT = "model_root"
SOURCE_BUILTIN = "builtin"
SOURCE_UNAVAILABLE = "unavailable"

#: 内置兜底映射：两字符一对（前者繁体、后者简体），按繁体码位升序拼接。
#: 由 OpenCC TSCharacters.txt 按本地中文语料高频子集筛选生成（638 对），覆盖第八轮
#: 真实 82 条 ASR 转写中 100% 的繁体字出现；仅当包内与 ``$MODEL_ROOT/opencc`` 词典
#: 都不可用时启用，保证「无网络 + 未部署词典」时仍能工作。
_BUILTIN_T2S_PAIRS = (
    "丟丢並并乾干亂乱佈布佔占併并來来侷局係系俬私倉仓個个們们偉伟側侧偽伪傘伞備备傢家傳传僅仅僞伪價价儀仪億亿儘尽償偿優优儲储兒儿內内兩两冊册冪幂凈净凍冻別别刪删則则剋克剛刚剝剥創创劃划動动務务勝胜勵励勻匀匯汇區区協协卻却卽即厤历參参員员唸念問问啓启啟启喫吃單单嗎吗嘗尝噹当嚇吓嚐尝嚮向嚴严國国圍围園园圖图團团埰采執执報报場场"
    "塊块壓压壞坏夠够夾夹奬奖學学寀采實实寧宁審审寫写寬宽將将專专尋寻對对導导屆届層层屬属峯峰嶺岭師师帶带幀帧幷并幹干幾几庫库廠厂廢废廣广張张強强彆别彈弹彔录彙汇後后徑径從从復复徵征徹彻恆恒態态慣惯慮虑憑凭應应懶懒戰战戶户拋抛拚拼捨舍捲卷掃扫掛挂採采換换損损搶抢摻掺撐撑擁拥擇择擊击擋挡擔担據据擠挤擡抬擬拟擴扩擾扰攔拦攜携攝摄"
    "攤摊攪搅敎教敗败敘叙數数斂敛斷断於于旣既昇升時时暢畅暫暂曆历曉晓曏向書书會会朮术東东柵栅査查條条棄弃棊棋棧栈業业極极榘矩榦干構构槓杠樁桩樓楼標标樣样樹树機机檔档檢检檯台檻槛欄栏權权歡欢歷历歸归殘残殺杀殻壳殼壳毀毁氣气氾泛汎泛汙污決决沒没沖冲況况泝溯洩泄淨净減减測测湊凑準准滙汇滯滞滾滚滿满漲涨潔洁潛潜潰溃濾滤瀏浏灣湾為为"
    "無无煉炼煙烟熱热燈灯燬毁爐炉爭争爲为爾尔牀床牴抵狀状獎奖獨独獲获獻献現现環环產产産产畢毕畫画異异畵画當当疊叠療疗癒愈癥症發发盃杯盡尽監监盤盘眞真睏困確确碼码礎础礦矿禦御種种稱称積积穩稳穫获窮穷竪竖競竞筆笔箇个節节範范篩筛簡简簽签籠笼籤签糰团糾纠紀纪約约紅红紋纹納纳純纯紕纰級级細细終终絃弦組组結结絕绝絡络給给統统絶绝綁绑"
    "經经綜综綠绿綫线維维綱纲網网綴缀綵彩綿绵緊紧緑绿緒绪線线緣缘編编緩缓緯纬練练緻致縮缩總总織织繞绕繪绘繫系繼继續续纍累纔才羅罗羣群義义習习翫玩聯联聲声職职聽听脈脉脩修脫脱脹胀腦脑腳脚臉脸臟脏臨临臺台與与舉举舊旧舖铺華华菸烟萬万蒐搜蓋盖蔘参薦荐藍蓝藝艺處处虛虚號号蟲虫術术衕同衛卫衝冲袷夹裏里補补裝装裡里製制複复覈核見见規规"
    "視视覺觉覽览觀观觸触訂订計计訊讯訓训託托記记訪访設设許许訴诉診诊註注証证評评詞词詢询試试話话該该詳详誌志認认誕诞語语誠诚誤误說说説说誰谁調调談谈請请論论諾诺謂谓講讲謝谢謹谨證证識识譜谱譟噪譭毁譯译議议護护讀读變变讓让豎竖豐丰貓猫貝贝負负貨货貪贪貫贯責责買买費费貼贴貿贸資资賦赋質质賬账賴赖購购跡迹踐践蹟迹蹤踪躍跃車车軌轨"
    "軍军軟软軸轴較较載载輔辅輕轻輛辆輪轮輯辑輸输轉转轎轿辦办辭辞迴回逕径這这連连週周進进遊游運运過过達达違违遙遥遞递遠远遡溯適适遲迟遶绕遷迁選选遺遗還还邊边邏逻郵邮鄰邻醫医釋释針针釦扣鈅钥鈎钩鈕钮鈡钟鉅巨鉤钩銜衔銳锐銷销鋪铺鋭锐錄录錨锚錯错録录錶表鍊炼鍵键鍼针鍾钟鎖锁鏇旋鏈链鏡镜鐘钟鐵铁鑑鉴鑒鉴鑰钥長长門门閉闭開开閑闲閒闲"
    "間间閘闸閤合閱阅閲阅閾阈闆板闇暗關关陞升陣阵陽阳隊队階阶際际隨随險险隱隐隻只雖虽雙双雜杂離离難难雲云電电霧雾靜静韆千響响頁页頂顶項项順顺須须預预頓顿領领頭头頻频題题額额類类顧顾顯显風风颱台飛飞飾饰餘余餬糊餵喂饋馈馬马駐驻駕驾駛驶驅驱驗验驟骤髒脏體体髮发鬆松鬚须鳥鸟鹽盐麥麦麪面麫面麵面麼么麽么黃黄點点齊齐齡龄齣出"
)


def _builtin_mapping() -> Dict[str, str]:
    """把紧凑的对偶字符串展开为 ``{繁体: 简体}``；格式非法时显式报错。"""
    packed = _BUILTIN_T2S_PAIRS
    if not packed:
        return {}
    if len(packed) % 2:
        raise RuntimeError("内置兜底映射长度必须为偶数：%d" % len(packed))
    return {packed[index]: packed[index + 1] for index in range(0, len(packed), 2)}


def parse_table(text: str) -> Dict[str, str]:
    """解析 OpenCC ``TSCharacters`` 文本；跳过注释/空行/非法行，取首个简体候选。"""
    mapping: Dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        key = parts[0].strip()
        candidates = parts[1].split()
        if len(key) != 1 or not candidates:
            continue
        value = candidates[0]
        if len(value) != 1:
            continue
        mapping[key] = value
    return mapping


def table_candidates() -> List[Tuple[str, Path]]:
    """离线词典候选（顺序即优先级）：包内自带 → ``$MODEL_ROOT/opencc``。"""
    package_table = Path(__file__).resolve().parent / "data" / _TABLE_FILENAME
    candidates: List[Tuple[str, Path]] = [(SOURCE_PACKAGE, package_table)]
    model_root = (os.environ.get("MODEL_ROOT") or "").strip()
    if model_root:
        candidates.append((SOURCE_MODEL_ROOT,
                           Path(model_root).expanduser() / "opencc" / _TABLE_FILENAME))
    return candidates


def load_table(path: Path) -> Dict[str, str]:
    """读取并校验一个本地词典文件；条目过少视为无效。"""
    mapping = parse_table(Path(path).read_text(encoding="utf-8"))
    if len(mapping) < _MIN_TABLE_ENTRIES:
        raise ValueError("映射表条目过少（%d < %d），疑似损坏或非 TSCharacters 格式"
                         % (len(mapping), _MIN_TABLE_ENTRIES))
    return mapping


def _resolve_mapping() -> Tuple[Dict[str, str], Dict[str, Any]]:
    """按优先级解析映射表与来源快照；全部文件失败时回落到内置映射。"""
    errors: List[str] = []
    for source, path in table_candidates():
        try:
            mapping = load_table(path)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append("%s: %s" % (path.name, exc))
            continue
        return mapping, {"source": source, "path": str(path), "error": None}
    mapping = _builtin_mapping()
    error = "；".join(errors) if errors else None
    source = SOURCE_BUILTIN if mapping else SOURCE_UNAVAILABLE
    return mapping, {"source": source, "path": None, "error": error}


_global_lock = threading.Lock()
_state: Dict[str, Any] = {"mapping": None, "snapshot": None, "translate": None}


def reset_cache() -> None:
    """清空已解析的映射缓存（供测试/部署热更新使用）。"""
    with _global_lock:
        _state["mapping"] = None
        _state["snapshot"] = None
        _state["translate"] = None


def _runtime() -> Tuple[Dict[str, str], Dict[str, Any], Dict[int, str]]:
    with _global_lock:
        if _state["mapping"] is None:
            mapping, snapshot = _resolve_mapping()
            _state["mapping"] = mapping
            _state["snapshot"] = snapshot
            _state["translate"] = {ord(source): target
                                   for source, target in mapping.items()
                                   if source != target}
        return _state["mapping"], _state["snapshot"], _state["translate"]


def is_enabled(value: Optional[bool] = None) -> bool:
    """把配置开关归一为布尔；``None`` 表示按默认（开启）处理。"""
    return DEFAULT_ENABLED if value is None else bool(value)


def to_simplified(text: Any) -> str:
    """逐字符把繁体转成简体；英文/数字/标点原样保留。"""
    if text is None:
        return ""
    value = str(text)
    if not value:
        return value
    _mapping, _snapshot, translate = _runtime()
    if not translate:
        return value
    return value.translate(translate)


def converted_char_count(text: Any) -> int:
    """统计文本中真正发生繁→简变化的字符数（用于输出审计）。"""
    if text is None:
        return 0
    mapping, _snapshot, _translate = _runtime()
    changed = 0
    for char in str(text):
        target = mapping.get(char)
        if target is not None and target != char:
            changed += 1
    return changed


def mapping_snapshot() -> Dict[str, Any]:
    """映射表来源快照：``source``/``path``/``entries``/``error``，供血缘与结果审计。"""
    mapping, snapshot, _translate = _runtime()
    result: Dict[str, Any] = dict(snapshot)
    result["entries"] = len(mapping)
    result["enabled"] = True
    return result
