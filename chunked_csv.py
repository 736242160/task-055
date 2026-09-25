#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chunked_csv.py —— 跨分块状态保持的流式“类 CSV”文本解析器（纯标准库，单文件）

规则
----
* 逻辑行 = 一条记录，字段用逗号分隔；
* 字段可用双引号包裹：引号内的逗号、换行原样保留，两个连续双引号("")表示一个字面引号；
* 行首出现三个反引号(```)时切换代码块状态，代码块内容不做 CSV 解析，原样输出；
* 引号 / 代码块 / 行首 fence 探测等状态全部跨分块保持，字段可正好跨分块边界；
* 输入结束时若引号或代码块未闭合，报告其起始位置（第几个分块、块内第几行，均从 1 计）；
* 分块大小不影响解析结果（自测里用多种块大小断言结果一致）。

直接运行：python3 chunked_csv.py —— 先打印演示，再跑全部自测。
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

Pos = Tuple[int, int]  # (分块号, 块内行号)，均从 1 开始

TEXT, CODE = "TEXT", "CODE"


@dataclass
class Record:
    kind: str          # "record" | "code" | "code_begin" | "code_end"
    fields: List[str]  # record: 解析出的字段；code: [原始行]；fence: ["```"]
    start: Pos
    end: Pos


@dataclass
class Unclosed:
    kind: str          # "quote" | "code_block"
    start: Pos


class ChunkedParser:
    """增量解析器：逐块 feed(text)，最后 finish() 收尾并报告未闭合状态。"""

    def __init__(self):
        self.mode = TEXT
        self.chunk_idx = 0          # 当前分块号（每 feed 一次 +1）
        self.line_no = 1            # 当前分块内的物理行号

        # ---- CSV 状态（跨块保持）----
        self.in_quotes = False
        self.quote_close_pending = False   # 引号内刚看到 "，等下一个字符才能判定
        self.quote_start: Optional[Pos] = None
        self.fields: List[str] = []
        self.field_buf: List[str] = []
        self.field_at_start = True
        self.record_start: Pos = (1, 1)

        # ---- 代码块状态（跨块保持）----
        self.code_start: Optional[Pos] = None
        self.code_line_buf: List[str] = []

        # ---- 行首 ``` 探测（``` 本身也可能被切块拆开）----
        self.at_line_start = True
        self.line_start_pending: List[str] = []
        self.skip_to_eol = False           # fence 行剩余部分（如语言名）忽略
        self.line_start_pos: Pos = (1, 1)

        self.prev_cr = False               # 上一字符是 \r（吞掉可能的 \n，可跨块）

    # ---------------- 对外接口 ----------------
    def feed(self, text: str) -> List[Record]:
        """喂入一个分块，返回本分块产生的记录。"""
        self.chunk_idx += 1
        self.line_no = 1
        out: List[Record] = []
        for ch in text:
            self._step(ch, out)
        return out

    def finish(self) -> Tuple[List[Record], Optional[Unclosed]]:
        """输入结束：冲刷残余，返回 (剩余记录, 未闭合报告或 None)。"""
        out: List[Record] = []
        # 行首不足 3 个的反引号，按普通内容冲刷
        if self.line_start_pending:
            pending = "".join(self.line_start_pending)
            self.line_start_pending = []
            self.at_line_start = False
            for ch in pending:
                self._mode_char(ch, out)
        # 结尾孤立的 " 视为闭合引号
        if self.quote_close_pending:
            self.quote_close_pending = False
            self.in_quotes = False
        unclosed = None
        if self.mode == TEXT:
            if self.in_quotes:
                unclosed = Unclosed("quote", self.quote_start)
            # 最后一条没有换行结尾的记录（含未闭合引号留下的残记录）
            if self.fields or self.field_buf or self.in_quotes:
                self._end_field()
                self._emit_record(out)
        else:
            if self.code_line_buf:
                self._emit_code_line(out)
            unclosed = Unclosed("code_block", self.code_start)
        return out, unclosed

    # ---------------- 状态机 ----------------
    def _step(self, ch: str, out: List[Record]):
        if self.prev_cr:
            self.prev_cr = False
            if ch == "\n":
                return                       # \r\n：\n 不再重复计数
        if self.skip_to_eol:
            if ch == "\n":
                self.skip_to_eol = False
                self._count_line()
            return
        if self.at_line_start:
            if ch == "`" and len(self.line_start_pending) < 3:
                self.line_start_pending.append("`")
                if len(self.line_start_pending) == 3:
                    self.line_start_pending = []
                    self._toggle_fence(out)
                    self.skip_to_eol = True
                    self.at_line_start = False
                return
            # 不是 fence：把缓存的反引号倒回正常处理
            self.at_line_start = False
            if self.line_start_pending:
                pending = "".join(self.line_start_pending)
                self.line_start_pending = []
                for p in pending:
                    self._mode_char(p, out)
        self._mode_char(ch, out)

    def _mode_char(self, ch: str, out: List[Record]):
        if self.mode == CODE:
            self._code_char(ch, out)
        else:
            self._csv_char(ch, out)

    # ---- CSV 模式 ----
    def _csv_char(self, ch: str, out: List[Record]):
        if self.quote_close_pending:
            # 引号内的 " 后面跟的字符决定："" 转义 / 否则引号闭合
            self.quote_close_pending = False
            self.in_quotes = False
            if ch == '"':
                self.field_buf.append('"')
                self.in_quotes = True
                return
        if self.in_quotes:
            if ch == '"':
                self.quote_close_pending = True
            else:
                self.field_buf.append(ch)
                if ch == "\n":
                    self.line_no += 1        # 引号内换行：只计物理行，不结束记录
            return
        if ch == ",":
            self._end_field()
        elif ch == '"':
            if self.field_at_start:
                self.field_at_start = False
                self.in_quotes = True
                self.quote_start = (self.chunk_idx, self.line_no)
            else:
                self.field_buf.append(ch)    # 字段中间的裸引号按字面处理
        elif ch == "\n":
            self._end_field()
            self._emit_record(out)
            self._count_line()
            self.record_start = (self.chunk_idx, self.line_no)
        elif ch == "\r":
            self._end_field()
            self._emit_record(out)
            self._count_line()
            self.record_start = (self.chunk_idx, self.line_no)
            self.prev_cr = True
        else:
            self.field_buf.append(ch)
            self.field_at_start = False

    # ---- 代码块模式 ----
    def _code_char(self, ch: str, out: List[Record]):
        if ch == "\n":
            self._emit_code_line(out)
            self._count_line()
        elif ch == "\r":
            self._emit_code_line(out)
            self._count_line()
            self.prev_cr = True
        else:
            self.code_line_buf.append(ch)

    # ---- 公共小工具 ----
    def _toggle_fence(self, out: List[Record]):
        pos = (self.chunk_idx, self.line_no)
        if self.mode == TEXT:
            self.mode = CODE
            self.code_start = pos
            out.append(Record("code_begin", ["```"], pos, pos))
        else:
            self.mode = TEXT
            out.append(Record("code_end", ["```"], self.code_start, pos))
            self.code_start = None

    def _count_line(self):
        self.line_no += 1
        self.at_line_start = True
        self.line_start_pos = (self.chunk_idx, self.line_no)

    def _end_field(self):
        self.fields.append("".join(self.field_buf))
        self.field_buf = []
        self.field_at_start = True

    def _emit_record(self, out: List[Record]):
        pos = (self.chunk_idx, self.line_no)
        out.append(Record("record", self.fields, self.record_start, pos))
        self.fields = []

    def _emit_code_line(self, out: List[Record]):
        pos = (self.chunk_idx, self.line_no)
        out.append(Record("code", ["".join(self.code_line_buf)],
                          self.line_start_pos, pos))
        self.code_line_buf = []


# ================= 演示与自测 =================

def run(chunks) -> Tuple[List[Record], Optional[Unclosed]]:
    p = ChunkedParser()
    events: List[Record] = []
    for c in chunks:
        events.extend(p.feed(c))
    tail, unclosed = p.finish()
    events.extend(tail)
    return events, unclosed


def split_chunks(text: str, size: int) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


def fmt(pos: Pos) -> str:
    return f"第{pos[0]}个分块第{pos[1]}行"


SAMPLE = (
    'name,remark\n'
    '"Alice","said ""hi"", then left"\n'
    '"Bob","first line\n'
    'second line"\n'
    '```python\n'
    'def f(a, b):\n'
    '    return a, b   # 逗号、"引号" 都不解析\n'
    '```\n'
    'Carol,"x,y"\n'
)

EXPECTED_SAMPLE = [
    ("record", ["name", "remark"]),
    ("record", ["Alice", 'said "hi", then left']),
    ("record", ["Bob", "first line\nsecond line"]),
    ("code_begin", ["```"]),
    ("code", ["def f(a, b):"]),
    ("code", ['    return a, b   # 逗号、"引号" 都不解析']),
    ("code_end", ["```"]),
    ("record", ["Carol", "x,y"]),
]


def demo():
    print("=" * 60)
    print("演示 1：按分块喂入（块大小=7），输出逻辑行处理结果")
    print("=" * 60)
    chunks = split_chunks(SAMPLE, 7)
    for i, c in enumerate(chunks, 1):
        print(f"  分块{i:>2}: {c!r}")
    events, unclosed = run(chunks)
    print("-" * 60)
    for ev in events:
        if ev.kind == "record":
            print(f"  记录   {fmt(ev.start)}~{fmt(ev.end)}  字段={ev.fields}")
        elif ev.kind == "code":
            print(f"  代码行 {fmt(ev.start)}  | {ev.fields[0]}")
        elif ev.kind == "code_begin":
            print(f"  代码块开始 @ {fmt(ev.start)}")
        else:
            print(f"  代码块结束 @ {fmt(ev.end)}")
    print(f"  未闭合报告: {unclosed}")

    print()
    print("=" * 60)
    print("演示 2：跨块定位 —— 未闭合的引号 / 代码块")
    print("=" * 60)
    chunks_q = ["a,b\n", "c,d\n", '"未闭合,引号\n', "仍在引号内\n"]
    events, unclosed = run(chunks_q)
    for i, c in enumerate(chunks_q, 1):
        print(f"  分块{i}: {c!r}")
    for ev in events:
        print(f"  记录 {fmt(ev.start)}~{fmt(ev.end)}  字段={ev.fields}")
    print(f"  >>> 未闭合的引号：起始于{fmt(unclosed.start)}")

    print()
    chunks_c = ["x,y\n", "```\n", 'code, "not parsed"\n']
    events, unclosed = run(chunks_c)
    for i, c in enumerate(chunks_c, 1):
        print(f"  分块{i}: {c!r}")
    for ev in events:
        print(f"  {ev.kind}: {ev.fields}")
    print(f"  >>> 未闭合的代码块：起始于{fmt(unclosed.start)}")


def test_sample():
    events, unclosed = run([SAMPLE])
    got = [(e.kind, e.fields) for e in events]
    assert got == EXPECTED_SAMPLE, got
    assert unclosed is None


def test_chunk_size_invariance():
    base = [(e.kind, e.fields) for e in run([SAMPLE])[0]]
    for size in (1, 2, 3, 4, 5, 7, 13, 64, 1000):
        got = [(e.kind, e.fields) for e in run(split_chunks(SAMPLE, size))[0]]
        assert got == base, f"块大小 {size} 结果不一致"
    # 不规则切分
    odd, idx = [], 0
    for n in (1, 5, 2, 9, 3, 30, 1, 4):
        odd.append(SAMPLE[idx:idx + n]); idx += n
    odd.append(SAMPLE[idx:])
    got = [(e.kind, e.fields) for e in run(odd)[0]]
    assert got == base, "不规则切分结果不一致"


def test_field_across_boundary():
    # 字段正好跨块边界
    events, _ = run(['"ab', 'cd",e\n'])
    assert [(e.kind, e.fields) for e in events] == [("record", ["abcd", "e"])]
    # 转义引号 "" 被切到两个块里
    events, _ = run(['"a"', '"b"\n'])
    assert [(e.kind, e.fields) for e in events] == [("record", ['a"b'])]
    # 引号内换行跨块
    events, _ = run(['"x\ny', '\nz"\n'])
    assert [(e.kind, e.fields) for e in events] == [("record", ["x\ny\nz"])]


def test_fence_across_boundary():
    # ``` 被拆到三个块里
    events, unclosed = run(['x\n`', '`', '`\ny\n'])
    kinds = [(e.kind, e.fields) for e in events]
    assert kinds == [("record", ["x"]), ("code_begin", ["```"]), ("code", ["y"])]
    assert unclosed is not None and unclosed.kind == "code_block"
    assert unclosed.start == (3, 1)  # fence 在第 3 个分块第 1 行完成


def test_unclosed_quote_location():
    chunks = ["a,b\n", "c,d\n", '"未闭合,引号\n', "仍在引号内\n"]
    events, unclosed = run(chunks)
    assert unclosed is not None and unclosed.kind == "quote"
    assert unclosed.start == (3, 1), unclosed
    # 残记录仍被抢救输出
    assert events[-1].kind == "record"
    assert events[-1].fields == ["未闭合,引号\n仍在引号内\n"]


def test_unclosed_code_block_location():
    chunks = ["x,y\n", "```\n", 'code, "not parsed"\n']
    events, unclosed = run(chunks)
    assert unclosed is not None and unclosed.kind == "code_block"
    assert unclosed.start == (2, 1), unclosed
    assert ("code", ['code, "not parsed"']) in [(e.kind, e.fields) for e in events]


def test_crlf():
    events, _ = run(["a,b\r\nc,d\r\n"])
    assert [(e.kind, e.fields) for e in events] == [
        ("record", ["a", "b"]), ("record", ["c", "d"])]
    # \r 和 \n 被切到不同块
    events, _ = run(["a,b\r", "\nc,d\n"])
    assert [(e.kind, e.fields) for e in events] == [
        ("record", ["a", "b"]), ("record", ["c", "d"])]


def test_backtick_not_fence():
    # 行首不足 3 个反引号按普通内容处理
    events, unclosed = run(["`a`,b\n", "``\n"])
    assert [(e.kind, e.fields) for e in events] == [
        ("record", ["`a`", "b"]), ("record", ["``"])]
    assert unclosed is None


def main():
    demo()
    print()
    print("=" * 60)
    print("自测")
    print("=" * 60)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"全部 {len(tests)} 项自测通过。")


if __name__ == "__main__":
    main()
