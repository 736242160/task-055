#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chunked_parser.py — 跨分块状态保持的逻辑行(CSV 风格)解析器, 纯标准库单文件。

规则:
  * 逻辑行为 CSV 风格: 逗号分隔字段, 换行结束一行(兼容 \\r\\n)。
  * 字段可用双引号包裹; 引号内的逗号、换行原样保留;
    引号内两个连续双引号("")表示一个字面双引号。
  * 三个反引号(```)出现在行首即围栏(可带后缀如 ```python),
    围栏之间的代码块内容不做 CSV 解析, 原样输出。
  * 引号 / 代码块状态跨分块保持; 字段跨分块边界自动拼接。
  * finish() 时若有未闭合的引号或代码块, 报告其起始位置(第几个分块的第几行)。
  * 分块大小不影响结果(自测里有不变量断言)。

用法:
  作为库:
      parser = ChunkedParser()
      for chunk in chunks:
          parser.feed(chunk)          # 每次 feed 计为一个分块
      warnings = parser.finish()      # 未闭合警告列表
      for kind, payload in parser.output():
          ...                          # ('row', [字段...]) / ('code', 行) / ('fence', 信息)

  作为命令行:
      python3 chunked_parser.py 文件 [-s 分块大小]
      cat 文件 | python3 chunked_parser.py -s 7
      python3 chunked_parser.py --demo     # 运行全部自测样例
"""

import argparse
import random
import sys


class ChunkedParser:
    """逐字符状态机; 所有跨分块状态都保存在实例字段里。"""

    def __init__(self):
        self._out = []               # 输出: ('row', [...]) | ('code', str) | ('fence', str)
        self._fields = []            # 当前逻辑行已完成的字段
        self._field = []             # 当前字段字符缓冲
        self._field_started = False  # 当前字段是否已开始(区分空字段与未开始)
        self._in_quote = False       # 是否在引号字段内
        self._quote_pending = False  # 引号字段内刚看到一个 " (可能是转义或结束)
        self._in_code = False        # 是否在代码块内
        self._in_fence_line = False  # 正在消费围栏行 ``` 之后的剩余部分
        self._fence_info = []        # 围栏行后缀(如语言名)
        self._at_line_start = True   # 是否处于物理行行首
        self._fence_buf = []         # 行首待定字符(检测 ```, 需要 3 字符前瞻)
        self._code_line = []         # 代码块内当前原始行
        self._pending_cr = False     # 普通状态下刚看到 \r (等待是否 \r\n)
        self.chunk_no = 0            # 当前分块编号(从 1 开始)
        self.line_no = 0             # 当前分块内的物理行号(从 1 开始)
        self._quote_start = None     # 未闭合引号的起始 (分块号, 行号)
        self._code_start = None      # 未闭合代码块的起始 (分块号, 行号)

    # ------------------------------------------------------------------ API

    def feed(self, text):
        """喂入一个分块。每次调用计为一个新分块, 分块内行号从 1 计。"""
        self.chunk_no += 1
        self.line_no = 1
        for ch in text:
            self._consume(ch)

    def finish(self):
        """输入结束: 冲刷缓冲, 返回未闭合警告列表(字符串)。"""
        if self._pending_cr:  # 结尾孤立的 \r 当作字面字符
            self._pending_cr = False
            self._field.append('\r')
            self._field_started = True
        if self._fence_buf:  # 行首不足 3 字符, 不是围栏
            buffered = ''.join(self._fence_buf)
            self._fence_buf = []
            if self._in_code:
                self._code_line.extend(buffered)
            else:
                for c in buffered:
                    self._consume_plain(c)
        if self._in_fence_line:
            self._out.append(('fence', ''.join(self._fence_info)))
            self._fence_info = []
            self._in_fence_line = False
        if self._in_code and self._code_line:
            self._out.append(('code', ''.join(self._code_line)))
            self._code_line = []
        if self._quote_pending:  # 结尾的 " 视为闭合引号
            self._quote_pending = False
            self._in_quote = False
            self._quote_start = None
        if self._fields or self._field or self._field_started:
            self._end_field()
            self._out.append(('row', self._fields))
            self._fields = []
        warnings = []
        if self._quote_start is not None:
            warnings.append('未闭合的引号: 起始于第 %d 个分块第 %d 行' % self._quote_start)
        if self._in_code and self._code_start is not None:
            warnings.append('未闭合的代码块: 起始于第 %d 个分块第 %d 行' % self._code_start)
        return warnings

    def output(self):
        """返回已产出的 [('row', [...]) | ('code', str) | ('fence', str)] 列表。"""
        return list(self._out)

    # ------------------------------------------------------------- 状态机

    def _consume(self, ch):
        if self._pending_cr:  # \r 后面不是 \n => \r 作为字面字符落盘
            self._pending_cr = False
            if ch != '\n':
                self._field.append('\r')
                self._field_started = True
                self._at_line_start = False
        if self._in_fence_line:
            self._consume_fence_line(ch)
        elif self._in_code:
            self._consume_code(ch)
        elif self._in_quote:
            self._consume_quoted(ch)
        elif self._at_line_start:
            self._fence_detect(ch)
        else:
            self._consume_plain(ch)

    def _consume_plain(self, ch):
        if ch == '\r':
            self._pending_cr = True
        elif ch == ',':
            self._end_field()
            self._at_line_start = False
        elif ch == '\n':
            if self._fields or self._field or self._field_started:
                self._end_field()
                self._out.append(('row', self._fields))
                self._fields = []
            # 纯空行跳过, 保持行首状态
            self._at_line_start = True
            self._advance_line()
        elif ch == '"' and not self._field_started:
            self._in_quote = True
            self._field_started = True
            self._quote_start = (self.chunk_no, self.line_no)
            self._at_line_start = False
        else:
            self._field.append(ch)
            self._field_started = True
            self._at_line_start = False

    def _consume_quoted(self, ch):
        if self._quote_pending:
            self._quote_pending = False
            if ch == '"':            # "" => 字面双引号
                self._field.append('"')
                return
            # 否则引号关闭, 当前字符按普通规则重新处理
            self._in_quote = False
            self._quote_start = None
            self._consume_plain(ch)
            return
        if ch == '"':
            self._quote_pending = True
        elif ch == '\n':             # 引号内换行: 属于字段内容, 但物理行号要推进
            self._field.append('\n')
            self._advance_line()
        else:
            self._field.append(ch)

    def _fence_detect(self, ch):
        """行首(非引号/非代码块): 缓冲至多 3 个字符判断是否为 ``` 围栏。"""
        if ch == '\n':               # 不足 3 字符就换行 => 不是围栏
            buffered = self._fence_buf
            self._fence_buf = []
            for c in buffered:
                self._consume_plain(c)
            self._consume_plain('\n')
            return
        self._fence_buf.append(ch)
        if len(self._fence_buf) < 3:
            return
        buffered = ''.join(self._fence_buf)
        self._fence_buf = []
        if buffered == '```':
            self._in_code = True
            self._in_fence_line = True
            self._code_start = (self.chunk_no, self.line_no)
            self._at_line_start = False
        else:
            for c in buffered:
                self._consume_plain(c)

    def _consume_fence_line(self, ch):
        if ch == '\n':
            self._out.append(('fence', ''.join(self._fence_info)))
            self._fence_info = []
            self._in_fence_line = False
            self._at_line_start = True
            self._advance_line()
        else:
            self._fence_info.append(ch)

    def _consume_code(self, ch):
        if self._at_line_start:      # 代码块内行首: 检测闭合围栏 ```
            if ch == '\n':
                buffered = ''.join(self._fence_buf)
                self._fence_buf = []
                self._out.append(('code', buffered))
                self._advance_line()
                return
            self._fence_buf.append(ch)
            if len(self._fence_buf) < 3:
                return
            buffered = ''.join(self._fence_buf)
            self._fence_buf = []
            if buffered == '```':
                self._in_code = False
                self._code_start = None
                self._in_fence_line = True
            else:                    # 不是围栏: 属于代码行内容
                self._code_line = list(buffered)
            self._at_line_start = False
            return
        if ch == '\n':
            self._out.append(('code', ''.join(self._code_line)))
            self._code_line = []
            self._at_line_start = True
            self._advance_line()
        else:
            self._code_line.append(ch)

    # ------------------------------------------------------------- 小工具

    def _end_field(self):
        self._fields.append(''.join(self._field))
        self._field = []
        self._field_started = False

    def _advance_line(self):
        self.line_no += 1


def render(output):
    """把解析输出渲染成易读文本。"""
    lines = []
    for kind, payload in output:
        if kind == 'row':
            lines.append('ROW   %r' % (payload,))
        elif kind == 'code':
            lines.append('CODE  %r' % payload)
        else:
            lines.append('FENCE %r' % payload)
    return '\n'.join(lines)


def run_chunks(chunks):
    """便捷函数: 喂入分块列表, 返回 (输出, 警告)。"""
    parser = ChunkedParser()
    for chunk in chunks:
        parser.feed(chunk)
    return parser.output(), parser.finish()


# ====================================================================== 自测

SAMPLE = (
    'name,remark\n'
    'alice,"hello, world"\n'
    'bob,"line1\nline2"\n'
    'carol,"say ""hi"""\n'
    '```python\n'
    'code, not parsed\n'
    '"a,b"\n'
    '```\n'
    'dave,plain\n'
)


def split_sizes(text, sizes):
    """按循环使用的大小列表切分文本。"""
    chunks, i, k = [], 0, 0
    while i < len(text):
        n = sizes[k % len(sizes)]
        chunks.append(text[i:i + n])
        i += n
        k += 1
    return chunks


def demo():
    print('=' * 70)
    print('样例 1: 分块喂入 + 引号/代码块跨块 + 字段跨块拼接')
    print('=' * 70)
    print('--- 原始文本 ---')
    print(SAMPLE)
    chunks = split_sizes(SAMPLE, [1, 17, 3, 26, 5])  # 故意用不规则小块
    print('--- 分块(大小 1/17/3/26/5 循环, 共 %d 块) ---' % len(chunks))
    for i, c in enumerate(chunks, 1):
        print('  块%-2d %r' % (i, c))
    out, warns = run_chunks(chunks)
    print('--- 解析结果 ---')
    print(render(out))
    print('--- 警告 ---')
    print('\n'.join(warns) if warns else '(无)')

    print()
    print('=' * 70)
    print('样例 2: 分块大小不变性(1/2/3/7/64/整体/随机 切分结果必须一致)')
    print('=' * 70)
    baseline, _ = run_chunks([SAMPLE])
    plannings = [[1], [2], [3], [7], [64], [len(SAMPLE)], [1, 17, 3, 26, 5]]
    rng = random.Random(42)
    plannings.append([rng.randint(1, 30) for _ in range(50)])
    for sizes in plannings:
        out2, warns2 = run_chunks(split_sizes(SAMPLE, sizes))
        assert out2 == baseline and not warns2, '分块大小 %s 结果不一致!' % sizes
        print('  分块方案 %-28s -> 一致 OK' % (str(sizes)[:26]))
    print('全部一致 OK')

    print()
    print('=' * 70)
    print('样例 3: 跨块定位 —— 未闭合引号/代码块报告起始分块与行号')
    print('=' * 70)
    cases = [
        ('引号在第 2 块第 1 行打开, 一直未闭合',
         ['a,b\n', 'c,"d\n', 'e,f\n']),
        ('引号在第 1 块第 2 行打开, 跨块后仍未闭合',
         ['x,"ab\n', 'cd",y\nz,"open']),
        ('代码块在第 1 块第 2 行打开, 未闭合',
         ['a,b\n```py\n', 'code1\ncode2\n']),
        ('代码块在第 2 块第 1 行打开, 未闭合',
         ['ok,fine\n', '```\n', 'x,y\n']),
    ]
    for desc, chunks in cases:
        out, warns = run_chunks(chunks)
        print('--- %s ---' % desc)
        for i, c in enumerate(chunks, 1):
            print('  块%-2d %r' % (i, c))
        print(render(out))
        for w in warns:
            print('  [警告] %s' % w)
        assert warns, '应当产生未闭合警告'
    print()
    print('全部自测通过。')


# ======================================================================= CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description='分块喂入的逻辑行(CSV 风格)解析器')
    ap.add_argument('file', nargs='?', help='输入文件(缺省读标准输入)')
    ap.add_argument('-s', '--chunk-size', type=int, default=4096, help='分块大小(默认 4096)')
    ap.add_argument('--demo', action='store_true', help='运行自测样例')
    args = ap.parse_args(argv)
    if args.demo:
        demo()
        return 0
    if args.file:
        with open(args.file, 'r', encoding='utf-8', newline='') as f:
            text = f.read()
    else:
        text = sys.stdin.buffer.read().decode('utf-8')
    size = max(1, args.chunk_size)
    out, warns = run_chunks(split_sizes(text, [size]))
    print(render(out))
    for w in warns:
        print('[警告] %s' % w, file=sys.stderr)
    return 1 if warns else 0


if __name__ == '__main__':
    sys.exit(main())
