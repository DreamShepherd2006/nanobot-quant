# -*- coding: utf-8 -*-
"""把 UTF-8 源码转换成 QMT 可加载的 GBK+CRLF 交付文件（带断言，避免静默出错）

用法：python3 qmt/build_deliver.py [name]
     name 默认 ashare_put_v5；改脚本不覆盖旧版，新版本另存为 ashare_put_v5.py 后
     运行 `python3 qmt/build_deliver.py ashare_put_v5` 生成对应交付文件
产物：qmt/deliver/<name>.py —— GBK 内容 + CRLF 换行 + 首行 `# -*- coding: gbk -*-`

为什么必须换首行声明：QMT 按首行声明选解码器。内容已转 GBK 而首行仍写 utf-8 时，
Python 会用 utf-8 解 GBK 字节，报
  SyntaxError: (unicode error) 'utf-8' codec can't decode byte 0xb9 ...
（2026-11-22 实际踩过：0xb9 是「A股」的 GBK 字节）
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = sys.argv[1] if len(sys.argv) > 1 else 'ashare_put_v5'
if NAME.endswith('.py'):
    NAME = NAME[:-3]
SRC = os.path.join(ROOT, NAME + '.py')
DST = os.path.join(ROOT, 'deliver', NAME + '.py')

HDR_UTF8 = '# -*- coding: utf-8 -*-'
HDR_GBK = '# -*- coding: gbk -*-'


def main():
    src = open(SRC, encoding='utf-8').read()
    lines = src.split('\n')
    assert lines[0].strip() == HDR_UTF8, '首行不是 utf-8 声明：%r' % lines[0]

    # GBK 可编码性：逐行报告，避免整份转换在某个 emoji 上炸掉
    bad = []
    for i, line in enumerate(lines, 1):
        try:
            line.encode('gbk')
        except UnicodeEncodeError as e:
            bad.append((i, str(e)[:100]))
    assert not bad, '有 %d 行不能 GBK 编码：%s' % (len(bad), bad[:5])

    lines[0] = HDR_GBK
    text = '\r\n'.join([l.replace('\r', '') for l in '\n'.join(lines).split('\n')])
    raw = text.encode('gbk')
    assert b'\n' not in raw.replace(b'\r\n', b''), '存在裸 LF（应为全 CRLF）'

    if not os.path.isdir(os.path.dirname(DST)):
        os.makedirs(os.path.dirname(DST))
    with open(DST, 'wb') as f:
        f.write(raw)

    # 回读校验：按声明解码 + 编译
    back = open(DST, 'rb').read()
    head = back.split(b'\r\n')[0]
    assert head == HDR_GBK.encode('gbk'), '首行声明未替换成 gbk'
    txt = back.decode('gbk')
    compile(txt, DST, 'exec')          # 语法自检（与 QMT 侧解码后一致）
    build = [l for l in txt.split('\r\n') if l.startswith('BUILD')]
    print('[OK] %s bytes=%d crlf=%d build=%s' % (
        DST, len(back), back.count(b'\r\n'), build[0] if build else '?'))


if __name__ == '__main__':
    sys.exit(main())
