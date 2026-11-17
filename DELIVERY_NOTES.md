# 交付说明：损坏 MFT 记录与损坏索引块的容错归位

## 1. 现象回顾

对掉电损坏的 NTFS 镜像做证据固定时，解析器对两类物理同源（撕裂写/坏扇区）的损坏表现分裂：

- **形态一（硬崩）**：文件记录（MFT record）枚举进行到一半直接崩溃，抛出的是 `ValueError`——一个与文件系统语义无关的通用异常。
- **形态二（静默）**：目录树枚举不崩溃，但平白少了条目，且无任何提示。
- 对照组：签名损坏的索引块（`INDX` magic 不符）被既有容错通道老老实实跳过。

## 2. 根因链路

### 2.1 既有容错通道（正常工作的部分）

库内所有解析异常都挂在 `dissect.ntfs.exceptions.Error` 之下。两条枚举主循环各有一个只捕获该族异常的容错点：

- `Mft.segments()`（`dissect/ntfs/mft.py`）：`except Error: continue`——跳过坏记录继续枚举；
- `Index.entries()`（`dissect/ntfs/index.py`）：`except Error: pass`——跳过坏索引块继续枚举。

签名检查（`FILE`/`INDX` magic）失败时抛出的 `BrokenMftError`/`BrokenIndexError` 属于该族，因此坏签名的记录和块能被正确跳过。这就是"既有跳过语义"。

### 2.2 形态一：硬崩的完整链路

```
Mft.segments()  (mft.py)                      ← 漏出调用点：只捕 except Error / EOFError
  └─ Mft.get(segment)
       └─ MftRecord.from_fh → MftRecord.from_bytes  (mft.py)
            ├─ data[:4] != b"FILE" → BrokenMftError   ← 签名检查，能被容错通道接住
            └─ apply_fixup(data)  (util.py)
                 └─ raise ValueError("Fixup offset out of range" / "Fixup mismatch")
                                                      ← 通用异常，不在 Error 族内，直接漏出
```

掉电撕裂写的典型特征是记录签名完好、但 fixup（更新序列号/USA）数据损坏。`apply_fixup` 是底层字节工具，用内置 `ValueError` 报告失败；它不在库的解析异常族内，`Mft.segments()` 的 `except Error` 接不住，枚举在第 N 条记录处整体崩掉，已枚举部分之外的证据全部丢失，且异常类型完全不提示这是解析问题。

同一漏出点也存在于索引侧：`IndexBuffer.__init__`（`index.py`）同样直接调用 `apply_fixup`，fixup 损坏的 `INDX` 块会让 `ValueError` 穿过 `Index.entries()` 的 `except Error` 崩掉目录枚举；`Index.search()`（`index.py` 第 112 行）下潜子节点时同样无防护。

### 2.3 形态二：静默丢失的完整链路

```
MftRecord.iterdir/listdir
  └─ Index.entries()  (index.py)
       ├─ except Error: pass        ← 吞掉 BrokenIndexError，无任何痕迹
       ├─ except EOFError: break    ← 读到流尾即整体退出，后续所有 VCN 一并丢弃，无痕迹
       └─ IndexBuffer.entries → _iter_entries()  (index.py)   ← 丢失发生在这一层
            ├─ except EOFError: break          ← 条目流中途截断：静默丢弃本块剩余条目
            ├─ entry.length 不加校验            ← 垃圾长度导致偏移乱跳、提前越界结束循环
            └─ Length == 0 视为 end            ← 损坏的零长度条目伪装成结束标记
```

形态二的损坏绕过了签名检查（`INDX` magic 与 fixup 均完好），坏在条目流内部。`_iter_entries` 对截断和垃圾长度不做任何校验，遇到即 `break`，该缓冲区剩余条目静默蒸发；`Index.entries` 的 `except Error: pass` 也不留任何日志。取证方无法区分"该目录只有这些条目"和"条目被损坏吞掉了"，证据完整性受损。

### 2.4 影响范围

- 所有经过 `Mft.segments()` 的全量记录枚举（取证遍历、 carving 前置步骤）：形态一下整体崩溃。
- 所有经过 `Index.entries()` 的目录列举（`iterdir`/`listdir`/目录树递归）：形态二下结果不完整且无提示；fixup 损坏的 `INDX` 块下同样崩溃。
- 所有直接调用 `MftRecord.from_bytes`/`Mft.get`/`IndexBuffer` 的上层用户：收到 `ValueError`，无法与真正的编程错误区分，也无法用 `except Error` 统一处理。
- 有效卷不受影响（修复前后枚举结果逐条一致，见第 5 节）。

## 3. 修复方案

### 3.1 异常类型归位

`apply_fixup` 保持抛出 `ValueError` 不变（它是无文件系统语义的纯字节工具，改它会波及所有下游库）；在两个调用点就地翻译为库自己的解析异常：

- `MftRecord.from_bytes`：`ValueError` → `BrokenMftError`（`mft.py`）；
- `IndexBuffer.__init__`：`ValueError` → `BrokenIndexError`（`index.py`）。

两者均为 `Error` 子类，于是两条容错通道无需新增任何捕获分支即可接住新损坏形态——这正是"两种损坏走同一条容错通道"。直接 `get()` 单条记录/单个块的上层调用方现在收到的也是 `Error` 族异常，可以统一 `except Error` 处理。

### 3.2 结构性校验（把形态二变成可检测的）

`_iter_entries` 增加两条不变式校验（NTFS 规范保证，且已在全部有效测试镜像上探针验证零误报）：

- 非结束条目的 `Length` 必须 ≥ `_INDEX_ENTRY` 头长（16 字节）且不越过条目流边界，否则抛 `BrokenIndexError`——垃圾长度不再能静默截断或带偏遍历；
- 条目流必然以 end 条目收尾，中途 EOF 即为截断/损坏，抛 `BrokenIndexError`——不再静默 `break`。

`Index.entries` 还新增对 `$INDEX_ALLOCATION` 提前 EOF 的检测：EOF 是枚举的正常退出条件（VCN 递增直到读穿流尾），但若 EOF 发生在流声明大小之内，说明分配流本身被截断，记 warning 后再退出。

### 3.3 追溯痕迹（日志分级及其理由）

实测有效镜像上，签名级跳过是**常态**而非损坏：mft.bin 有 220 个未分配/陈旧记录段、ntfs.bin 有 15 个陈旧 `INDX` 块被既有通道跳过。若对每次跳过都记 warning，有效卷枚举会被日志淹没，痕迹反而不可用。因此按"是否通过签名检查"分级：

- **签名级跳过**（既有语义，例行）：跳过点（`Mft.segments`、`Index.entries`）记 `DEBUG`，需要完整审计时开 debug 日志即可拿到逐条清单；
- **签名完好但内容损坏**（本次新纳入容错的形态，意味着真实证据损失）：在检测点记 `WARNING`——fixup 损坏的记录/块、条目流截断、非法条目长度、索引分配提前 EOF。有效卷上探针验证这些路径零触发，不会误报。

### 3.4 有意保留的行为（取舍说明）

- **`Index.search()` 不吞异常**：按路径精确查找时若子节点块损坏，`BrokenIndexError` 继续向上抛。对取证而言，"查找响亮失败"远优于"静默返回找不到"——后者会制造文件不存在的假阴性证据。
- **`$INDEX_ROOT` 损坏不跳过**：根索引流损坏时 `BrokenIndexError` 向上抛。根损坏意味着整个索引不可信，跳过整个目录而不报警比抛错更糟。
- **`Length == 0` 仍视为 end 条目**：这是库既有的防御性语义，有效卷上不会触发歧义，不改动以免改变有效卷行为。
- **跳过语义本身不变**：损坏记录/块仍然是"跳过该单元、继续枚举"，未引入任何重试或修复逻辑——只补检测、归位异常、留痕迹。

### 3.5 已知边界

`TotalSizeOfEntries` 被改小一类的损坏在局部不可检出（缓冲区自描述的大小本身就不可信时，无法仅凭本块判断条目被截断），需要交叉引用 B 树结构才能发现，超出本次范围；此类损坏的条目丢失仍静默，已在本文档中声明。

## 4. 回归用例

新增 4 个用例（`tests/test_mft.py`、`tests/test_index.py`），覆盖两类损坏形态：

- `test_mft_segments_broken_fixup_record`：fixup 损坏的 MFT 记录——枚举不崩、坏记录被跳过、warning 留痕、直接 `get()` 抛 `BrokenMftError` 而非 `ValueError`；
- `test_index_broken_buffer_fixup`：fixup 损坏的 `INDX` 块——目录枚举不崩、坏块按既有语义跳过、warning 留痕；
- `test_index_broken_entry_stream`：条目流内部损坏（非法条目长度）——同上；
- `test_index_broken_buffer_signature`：对照组——坏签名块仍按原语义静默跳过，不产生 warning（防日志淹没回归）。

## 5. 验收结果

- **有效卷枚举逐条一致**：对 ntfs.bin / mft.bin / ntfs-cloud.bin 三个有效镜像做全量段枚举 + 目录树递归枚举，修复前后结果序列化后 SHA-256 完全一致（`7bf1e5a5…e2572`），不多不少；
- **有效卷零 warning**：上述镜像枚举全程无任何 warning 级日志；
- **全量测试**：`pytest tests` 40 passed（36 既有 + 4 新增），本机约 5 分钟；
- **Lint**：`ruff check` / `ruff format --check` 通过（`dissect/ntfs/secure.py` 的 FURB171 为既有问题，与本次改动无关，未触碰）。

## 6. 改动清单

| 文件 | 改动 |
| --- | --- |
| `dissect/ntfs/mft.py` | `from_bytes` 将 fixup `ValueError` 翻译为 `BrokenMftError` 并记 warning；`segments` 跳过点记 debug |
| `dissect/ntfs/index.py` | `IndexBuffer.__init__` 将 fixup `ValueError` 翻译为 `BrokenIndexError` 并记 warning；`_iter_entries` 增加条目长度/截断校验（抛 `BrokenIndexError` 并记 warning）；`Index.entries` 跳过点记 debug、索引分配提前 EOF 记 warning |
| `tests/test_mft.py` | 新增 `test_mft_segments_broken_fixup_record` |
| `tests/test_index.py` | 新增 3 个索引损坏回归用例 |
