import re

from . import lark_all_pb2 as L
from . import proto_pb2 as P
from .ids import generate_request_cid


def wrap_packet(cmd: int, payload_msg, request_id: str) -> P.Packet:
    pkt = P.Packet()
    pkt.payloadType = 1
    pkt.cmd = cmd
    pkt.cid = request_id
    pkt.payload = payload_msg.SerializeToString()
    return pkt


LINK_RE = re.compile(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)')


def _fill_rich_text(rt, text: str, rich_links: bool):
    """把文本填进 richText:rich_links 时 [文字](url) 变成带文字的超链接元素(tag A),其余为文本元素。
    实测网页协议的 TEXT 消息接受多个 TEXT/A 元素;加粗、段落、POST 类型都会被服务端拒绝(400)。"""
    n = [0]
    def nid():
        n[0] += 1
        return str(n[0])
    def add_text(s):
        if not s:
            return
        i = nid(); el = rt.elements.dictionary[i]; el.tag = 1
        tp = P.TextProperty(); tp.content = s; el.property = tp.SerializeToString()
        rt.elementIds.append(i)
    def add_anchor(label, href):
        i = nid(); el = rt.elements.dictionary[i]; el.tag = 6
        ap = L.entities.RichTextElement.AnchorProperty(); ap.href = href; ap.content = label
        el.property = ap.SerializeToString()
        rt.elementIds.append(i); rt.anchorIds.append(i)
    if not rich_links:
        add_text(text)
    else:
        pos = 0
        for m in LINK_RE.finditer(text):
            add_text(text[pos:m.start()]); add_anchor(m.group(1), m.group(2)); pos = m.end()
        add_text(text[pos:])
    rt.innerText = text


def build_send_message_packet(text: str, chat_id: str, request_id: str, root_id: str=None, thread_chat: bool=False, reply_to: str=None, rich_links: bool=False) -> P.Packet:
    """root_id: 回复进话题(根消息 id); reply_to: 引用回复某条消息(parentId 指向它, rootId 指向它所在的根, 不设 isReplyInThread 时就是普通的引用样式)。"""
    cid_1 = generate_request_cid()
    req = P.PutMessageRequest()
    req.type = 4
    req.chatId = str(chat_id)
    req.cid = cid_1
    req.isNotified = 1
    req.version = 1
    if reply_to and (not root_id or str(root_id) == str(reply_to)):
        # 引用回复:parentId=rootId=被引用的消息,落在主会话里显示为引用
        req.rootId = str(reply_to)
        req.parentId = str(reply_to)
    elif root_id:
        req.rootId = str(root_id)
        req.parentId = str(reply_to or root_id)
        # 普通群缺 isReplyInThread 会落进主会话;话题群反而拒收它(replyInThread not support chat)
        if not thread_chat:
            req.isReplyInThread = True
    _fill_rich_text(req.content.richText, str(text), rich_links)
    return wrap_packet(5, req, request_id)


def _fill_rich_blocks(rt, blocks: list, inner_text: str):
    """按结构化 spec 填 richText(POST 用)。spec:
    blocks: [{type:'p', runs:[run]}, {type:'ul', items:[[run]]}, {type:'quote', blocks:[block]}, {type:'blank'}]
    run: {text, bold?} | {link:{text, href}} | {at:{user_id, text}}
    实测要点:容器元素(p/ul/li/quote)必须显式带 property 字段(哪怕为空),否则服务端报 unmarshal failed;
    不要包 DOCS 根节点;innerText 必填;加粗用 TEXT 元素的 style fontWeight + styles/styleRefs。"""
    counter = [0]
    used_bold = [False]
    def nid():
        counter[0] += 1
        return str(counter[0])
    def el(tag, children=(), prop=b'', style=None):
        i = nid(); e = rt.elements.dictionary[i]; e.tag = tag
        for ch in children:
            e.childIds.append(ch)
        e.property = prop
        if style:
            for k, v in style.items():
                e.style[k] = v
        return i
    def run_el(run):
        if 'link' in run:
            ap = L.entities.RichTextElement.AnchorProperty()
            ap.href = str(run['link']['href']); ap.content = str(run['link'].get('text') or run['link']['href'])
            i = el(6, prop=ap.SerializeToString()); rt.anchorIds.append(i); return i
        if 'at' in run:
            at = P.AtProperty(); at.userId = str(run['at']['user_id']); at.content = str(run['at'].get('text') or '')
            i = el(5, prop=at.SerializeToString()); rt.atIds.append(i); return i
        tp = P.TextProperty(); tp.content = str(run.get('text', ''))
        bold = bool(run.get('bold'))
        used_bold[0] = used_bold[0] or bold
        return el(1, prop=tp.SerializeToString(), style={'fontWeight': 'bold'} if bold else None)
    def block_el(block):
        t = block.get('type', 'p')
        if t == 'blank':
            return el(3)
        if t == 'p':
            return el(3, [run_el(r) for r in block.get('runs', [])])
        if t == 'ul':
            items = [el(28, [run_el(r) for r in runs]) for runs in block.get('items', [])]
            return el(26, items, prop=bytes.fromhex('0800'))
        if t == 'quote':
            return el(29, [block_el(b) for b in block.get('blocks', [])])
        raise ValueError(f'未知的富文本块类型: {t}')
    for b in blocks:
        rt.elementIds.append(block_el(b))
    if used_bold[0]:
        st = rt.elements.styles.add(); st.name = 'fontWeight'; st.value = 'bold'
        rt.elements.styleRefs['fontWeight'].styleIds.append(0)
    rt.innerText = inner_text
    rt.version = 1


def build_rich_message_packet(spec: dict, inner_text: str, chat_id: str, request_id: str, root_id: str=None, thread_chat: bool=False, reply_to: str=None) -> P.Packet:
    """结构化富文本(POST)消息,spec 见 _fill_rich_blocks;inner_text 是纯文本摘要(通知/搜索用)。"""
    req = P.PutMessageRequest()
    req.type = 2
    req.chatId = str(chat_id)
    req.cid = generate_request_cid()
    req.isNotified = 1
    req.version = 1
    if reply_to and (not root_id or str(root_id) == str(reply_to)):
        req.rootId = str(reply_to)
        req.parentId = str(reply_to)
    elif root_id:
        req.rootId = str(root_id)
        req.parentId = str(reply_to or root_id)
        if not thread_chat:
            req.isReplyInThread = True
    _fill_rich_blocks(req.content.richText, spec.get('blocks', []), inner_text)
    return wrap_packet(5, req, request_id)


def build_post_message_packet(html: str, chat_id: str, request_id: str, title: str='', root_id: str=None, thread_chat: bool=False, reply_to: str=None) -> P.Packet:
    """富文本(POST)消息:content.text 直接放 HTML 片段,服务端自行解析为富文本元素。
    实测只保留 <p> <a href>;<b>/<i> 在客户端显示为"当前版本不支持",列表/引用被丢弃。结构化的用 build_rich_message_packet。"""
    req = P.PutMessageRequest()
    req.type = 2
    req.chatId = str(chat_id)
    req.cid = generate_request_cid()
    req.isNotified = 1
    req.version = 1
    if reply_to and (not root_id or str(root_id) == str(reply_to)):
        req.rootId = str(reply_to)
        req.parentId = str(reply_to)
    elif root_id:
        req.rootId = str(root_id)
        req.parentId = str(reply_to or root_id)
        if not thread_chat:
            req.isReplyInThread = True
    if title:
        req.content.title = title
    req.content.text = html
    return wrap_packet(5, req, request_id)


def build_create_chat_packet(user_id: str, request_id: str) -> P.Packet:
    req = P.PutChatRequest()
    req.type = 1
    req.chatterIds.append(str(user_id))
    return wrap_packet(13, req, request_id)


def build_search_packet(query: str, request_id: str, locale: str='zh_CN') -> P.Packet:
    req = P.UniversalSearchRequest()
    req.header.searchSession = generate_request_cid()
    req.header.sessionSeqId = 1
    req.header.query = query
    req.header.searchContext.tagName = 'SMART_SEARCH'
    item1 = P.EntityItem()
    item1.type = 1
    item2 = P.EntityItem()
    item2.type = 2
    item2.filter.CopyFrom(P.EntityItem.EntityFilter())
    item3 = P.EntityItem()
    item3.type = 3
    item3.filter.groupChatFilter.CopyFrom(P.GroupChatFilter())
    item4 = P.EntityItem()
    item4.type = 10
    item4.filter.CopyFrom(P.EntityItem.EntityFilter())
    req.header.searchContext.entityItems.append(item1)
    req.header.searchContext.entityItems.append(item2)
    req.header.searchContext.entityItems.append(item3)
    req.header.searchContext.entityItems.append(item4)
    req.header.searchContext.commonFilter.includeOuterTenant = 1
    req.header.searchContext.sourceKey = 'messenger'
    req.header.locale = locale
    req.header.extraParam.CopyFrom(P.SearchExtraParam())
    return wrap_packet(11021, req, request_id)


def build_user_info_packet(user_id: str, chat_id: str, request_id: str) -> P.Packet:
    req = P.GetUserInfoRequest()
    req.userId = int(user_id)
    req.chatId = int(chat_id)
    req.userType = 1
    return wrap_packet(5023, req, request_id)


def build_group_info_packet(chat_id: str, request_id: str) -> P.Packet:
    req = P.GetGroupInfoRequest()
    req.chatId = str(chat_id)
    return wrap_packet(64, req, request_id)


def decode_put_message_response(content: bytes) -> str:
    pkt = P.Packet()
    pkt.ParseFromString(content)
    resp = P.PutMessageResponse()
    resp.ParseFromString(pkt.payload)
    return resp.message.id


def decode_put_chat_response(content: bytes):
    pkt = P.Packet()
    pkt.ParseFromString(content)
    if pkt.payload:
        resp = P.PutChatResponse()
        resp.ParseFromString(pkt.payload)
        return resp.chat.id
    return None


def decode_search_response(content: bytes):
    pkt = P.Packet()
    pkt.ParseFromString(content)
    results = []
    if pkt.payload:
        resp = P.UniversalSearchResponse()
        resp.ParseFromString(pkt.payload)
        for r in resp.results:
            if r.type == 1:
                results.append({'type': 'user', 'id': r.id, 'title': r.titleHighlighted})
            elif r.type == 3:
                results.append({'type': 'group', 'id': r.id, 'title': r.titleHighlighted})
    return results


def decode_user_info_response(content: bytes, locale: str='zh_CN'):
    pkt = P.Packet()
    pkt.ParseFromString(content)
    if not pkt.payload:
        return None
    info = P.UserInfo()
    info.ParseFromString(pkt.payload)
    detail = info.userInfoDetail.detail
    name = None
    for item in detail.locales:
        if item.key_string == locale.lower():
            return item.translation
    if detail.nickname:
        try:
            return detail.nickname.decode('utf-8')
        except Exception:
            return str(detail.nickname)
    return name


def decode_group_info_response(content: bytes):
    pkt = P.Packet()
    pkt.ParseFromString(content)
    if not pkt.payload:
        return None
    info = P.UserInfo()
    info.ParseFromString(pkt.payload)
    detail = info.userInfoDetail.detail
    for field in (detail.nickname1, detail.nickname4):
        if field:
            try:
                return field.decode('utf-8')
            except Exception:
                pass
    return None
