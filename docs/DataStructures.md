# Helldivers 2 / Stingray 资源格式分析

> 基于 `HD2SDK-CommunityEdition` Blender 插件源码（Python）逆向整理。
> 覆盖三大主题：**封包 (Package/Archive)**、**模型 (Unit/Mesh)** 与 **贴图 (Texture)**，
> 并附带与之强相关的材质、骨骼、动画、状态机、复合体（CompositeMesh）等数据结构。
>
> 所有字段顺序与 `Serialize()` 函数中的读写顺序一致，全部 **小端 (little-endian)**。
> 关键源码位置以 `file:line` 形式给出，方便回溯。

---

## 0. 通用基础

### 0.1 字节流 `MemoryStream`

源文件：`utils/memoryStream.py`

- 提供 `int8/uint8/int16/uint16/int32/uint32/int64/uint64`、`float16/float32/float64`，
  以及 `vec2/3/4_(float|half|uint8|uint16|uint32)` 系列对偶读/写接口。
- 同一个 `Serialize()` 函数既用于解析又用于打包：通过 `IsReading()` / `IsWriting()` 切换分支。
- 提供 10-bit-packed normal 编解码：`TenBitSigned / TenBitUnsigned / MakeTenBitSigned / MakeTenBitUnsigned`，
  用于 `VEC4_1010102` 顶点格式。

### 0.2 哈希

源文件：`utils/hashing.py`

- `murmur64_hash(data, seed=0)` — 标准 MurmurHash2 64 位，常量 `m=0xc6a4a7935bd1e995`, `r=47`。
- `murmur32_hash(data, seed=0)` — 取 `murmur64_hash` 高 32 位。
- 用途：
  - 文件 ID / 类型 ID 由 `murmur64(name)` 生成（FileID/TypeID 都是 `uint64`）。
  - 骨骼名 → `murmur32` 哈希，存入 `BoneHashes` / `TransformInfo.NameHashes`。

### 0.3 资源 / 类型 ID 常量

源文件：`utils/constants.py`

| 资源类型 | 类型 ID (uint64) |
|---|---|
| Unit (模型) | `16187218042980615487` (`0xE0A48D0BE9A7453F`) |
| CompositeUnit | `14191111524867688662` |
| Texture | `14790446551990181426` (`0xCD4238C6A0C69E32`) |
| Material | `16915718763308572383` (`0xEAC0B497876ADEDF`) |
| Bone | `1792059921637536489` (`0x18DEAD01056B72E9`) |
| Animation | `10600967118105529382` (`0x931E336D7646CC26`) |
| StateMachine | `11855396184103720540` (`0xA486D4045106165C`) |
| Particle | `12112766700566326628` (`0xA8193123526FAD64`) |
| WwiseBank / Dep / Stream / Meta | 见 `constants.py` |

`Global_TypeIDs` 列出了全部已知 50+ 种资源类型；`Global_MaterialParentIDs` 是材质模板 → 名称的映射。

---

## 1. 封包系统 (Archive / Package)

游戏数据目录形如：
```
<game>/data/9ba626afa44a3aa3            ← base archive (BaseArchiveHexID)
<game>/data/<hash>                       ← toc 主文件
<game>/data/<hash>.gpu_resources         ← GPU 资源 (顶点/索引/贴图主数据)
<game>/data/<hash>.stream                ← 流式资源 (大贴图、音频)
<game>/data/<hash>.patch_N (+...) ...    ← 补丁 patch
<game>/data/bundles.nxa, bundles.NN.nxa  ← Slim 版本的捆绑包
```

源文件：`__init__.py`（`TocEntry / TocFileType / SearchToc / StreamToc / TocManager`）+ `utils/slim.py`（解压与 bundle 处理）。

### 1.1 三种包形态

由 `utils/slim.py:get_package_toc / load_package` 探测：

```
UNCOMPRESSED LEGACY  : 直接打开 <name>，magic == 0xF0000004 (4026531857)
COMPRESSED DSAR      : magic == 0x52415344 ("DSAR", 1380012868)，整文件 LZ4 分块压缩
BUNDLED              : 文件不存在于 data/ 下，必须从 bundles.nxa + bundles.NN.nxa 中提取
```

### 1.2 LEGACY / 未压缩 TOC（核心容器格式）

#### 1.2.1 文件头 `StreamToc` (`__init__.py:738-801`)

| 偏移 | 类型 | 字段 | 备注 |
|---|---|---|---|
| 0x00 | uint32 | `magic` | 必须为 `0xF0000004` |
| 0x04 | uint32 | `numTypes` | 类型表条目数 |
| 0x08 | uint32 | `numFiles` | 文件表条目数 |
| 0x0C | uint32 | `unknown` | — |
| 0x10 | bytes[56] | `unk4Data` | 头部填充/保留区 |
| 0x40 | `TocFileType[numTypes]` | 类型表（每项 32 字节） | |
| ...  | `TocEntry[numFiles]`     | 文件条目表（每项 80 字节） | |
| ...  | toc / gpu / stream 数据 | 按 `TocEntry` 中的偏移定位 |

写入时若文件总长 < `256 * numFiles`，会用 0 填充至该长度（最小尺寸约束）。

#### 1.2.2 类型表项 `TocFileType` (`__init__.py:642-655`, 共 32 字节)

| 类型 | 字段 | 备注 |
|---|---|---|
| uint64 | `unk1` | 0 |
| uint64 | `TypeID` | 资源类型 hash (`UnitID`, `TexID`…) |
| uint64 | `NumFiles` | 该类型下的文件数 |
| uint32 | `unk2` | 通常 16 |
| uint32 | `unk3` | 通常 64 |

#### 1.2.3 文件条目 `TocEntry` (`__init__.py:514-549`, 共 80 字节)

| 偏移 | 类型 | 字段 | 含义 |
|---|---|---|---|
| 0x00 | uint64 | `FileID` | murmur64(资源名) |
| 0x08 | uint64 | `TypeID` | 见 `Global_TypeIDs` |
| 0x10 | uint64 | `TocDataOffset` | 主数据在 toc 文件中的偏移 |
| 0x18 | uint64 | `StreamOffset` | 在 `.stream` 中的偏移 |
| 0x20 | uint64 | `GpuResourceOffset` | 在 `.gpu_resources` 中的偏移 |
| 0x28 | uint64 | `Unknown1` | — |
| 0x30 | uint64 | `Unknown2` | — |
| 0x38 | uint32 | `TocDataSize` | toc 区块大小 |
| 0x3C | uint32 | `StreamSize` | stream 区块大小 |
| 0x40 | uint32 | `GpuResourceSize` | gpu_resources 区块大小 |
| 0x44 | uint32 | `Unknown3` | 通常 16 |
| 0x48 | uint32 | `Unknown4` | 通常 64 |
| 0x4C | uint32 | `EntryIndex` | 写入时递增填充 |

写入时数据布局规则（`TocEntry.SerializeData`）：
- `GpuResourceOffset` 对齐到 64 字节边界。
- `StreamOffset` 对齐到 64 字节边界。

### 1.3 DSAR 压缩包 (`utils/slim.py:57-87`)

DSAR 文件头：
```
0x00 uint32 magic = 0x52415344
0x04 uint32 ?
0x08 uint32 num_chunks
0x0C-0x1F ...
0x20 + i*0x20   chunk[i] header (32 字节)
```

每个 chunk header (32 字节):
| 类型 | 字段 |
|---|---|
| uint64 | `uncompressed_offset` |
| uint64 | `compressed_offset` |
| uint32 | `uncompressed_size` |
| uint32 | `compressed_size` |
| uint8  | `compression_type` (`0=UNCOMPRESSED`, `3=LZ4`) |
| uint8  | `chunk_type` (位域: `START=0x02, CONTINUE=0x04, UNK=0x01`) |
| bytes[6] | pad |

LZ4 用块模式 (`lz4.block.decompress`)。整包顺序还原后即为 LEGACY 格式的 toc/gpu/stream。

### 1.4 Bundle / Slim 版本 (`utils/slim.py:125-330`)

游戏“Slim”版本（缺失 `9ba626afa44a3aa3`）下，资源被打包到 `bundles.nxa` + 多个 `bundles.NN.nxa`。

- `bundles.nxa` 起头自身是 DSAR，解压后是一张索引表：
  - 0x0C..0x10 = `num_bundles`
  - 0x10..0x14 = `num_packages`
  - 0x18 起每 0x18 字节为一个 `package`：
    ```
    uint64 bundle_size
    uint32 name_offset    (指向以 0x00 结尾的 ASCII 名)
    uint32 items_count
    uint32 items_offset
    ```
  - `items` 每项 0x10 字节 = `BundleEntry`:
    ```
    uint64 original_archive_offset  (在还原后的逻辑 package 内的偏移)
    uint32 uncompressed_bundle_offset (在 bundles.NN.nxa 解压流中的偏移)
    uint8  pad[3]
    uint8  bundle_index             (.nxa 编号)
    ```
- 还原过程：`reconstruct_package_from_bundles` 按 `BundleEntry` 依次从对应 `bundles.NN.nxa`（同样是 DSAR 块结构）取出片段，按 `original_archive_offset` 拼回完整 package。

### 1.5 SearchToc（轻量索引）

`SearchToc` 不解析具体数据，只读取头部和文件条目（每条 80 字节），把 `(TypeID -> [FileID])` 索引到字典里，用于跨多个 archive 快速搜索引用。`FromFile`/`FromSlimFile`/`FromPackage` 分别对应三种来源。

### 1.6 Patch 管理 (`TocManager`)

- 多个 archive 在内存中以 `TocDict[TypeID][FileID] = TocEntry` 形式管理。
- 编辑模型/贴图/材质时，会把对应 `TocEntry` 复制进 `ActivePatch`，写出时生成 `*.patch_N` 文件，结构与 LEGACY toc 完全一致。

---

## 2. 模型系统 (Unit / Mesh)

资源类型 ID = `UnitID = 0xE0A48D0BE9A7453F`。
顶层数据结构：`stingray/unit.py:StingrayMeshFile`。

整个 Unit 数据被拆成两个流：
- **toc 流** — 文件头、引用、骨骼信息、StreamInfo、MeshInfo、材质槽……（即 `TocEntry.TocData`）
- **gpu 流** — 顶点缓冲 + 索引缓冲的原始字节（即 `TocEntry.GpuData`）

### 2.1 顶层 `StingrayMeshFile` (`stingray/unit.py:20-704`)

文件头按写入顺序：

| 偏移(读时) | 类型 | 字段 | 备注 |
|---|---|---|---|
| 0 | uint64 | `UnkRef1` | 未知引用 |
| 8 | uint64 | `BonesRef` | 关联 Bone 资源的 FileID |
| 16 | uint64 | `CompositeRef` | 关联 CompositeUnit 资源；写入时强制 0 |
| 24 | uint64 | `UnkRef2` | |
| 32 | uint64 | `StateMachineRef` | 关联 StateMachine 资源 |
| 40 | bytes[28] | `HeaderData1` | 用 `uint32` 序列化（其实只走头 4 字节） |
| 44 | uint32 | `Version` | 已知 `10800437`（旧版）/ `10800438`（新版），影响顶点格式枚举 |
| 48 | uint32 | `UnreversedLODGroupListDataOffset` | |
| 52 | uint32 | `TransformInfoOffset` | |
| 56 | uint32 | `LightListOffset` | |
| 60 | uint32 | `UnkPreLightListOffset` | |
| 64 | uint32 | `WwiseCallbackOffset` | |
| 68 | bytes[8] | `HeaderData2` | |
| 76 | uint32 | `CustomizationInfoOffset` | |
| 80 | uint32 | `UnkHeaderOffset1` | |
| 84 | uint32 | `ConnectingBoneHashOffset` | |
| 88 | uint32 | `BoneInfoOffset` | |
| 92 | uint32 | `StreamInfoOffset` | |
| 96 | uint32 | `EndingOffset` | |
| 100 | uint32 | `MeshInfoOffset` | |
| 104 | uint64 | `HeaderUnk` | |
| 112 | uint32 | `MaterialsOffset` | |
| 116 | bytes[12] | pad | `f.seek(+12)` |

读取一旦发现 `MeshInfoOffset == 0` 或同时 `StreamInfoOffset == 0 && CompositeRef == 0`，会按 `SkipMeshImportErrors` 标志跳过或抛“Unsupported Mesh Format”。

各偏移指向的区块依次为：

1. **WwiseCallbackOffset → UnreversedWwiseCallbackData** —— 仍未逆向，按相邻偏移差值整段缓存。
2. **UnkPreLightListOffset → UnreversedPreLightListData**
3. **LightListOffset → LightList**（详见 2.7）
4. **UnreversedLODGroupListData** —— LOD 组列表（未逆向）
5. **TransformInfo**（2.5）
6. **CustomizationInfo**（2.6，只读）
7. **UnkHeaderData1 / UnreversedConnectingBoneData**
8. **BoneInfo[]**（2.2）
9. **StreamInfo[]**（2.3）
10. **MeshInfo[]**（2.4）
11. **Materials slot** ：`NumMaterials(uint32) + SectionsIDs[uint32] + MaterialIDs[uint64]`
12. **UnReversedData2** + `EndingBytes(uint64 = NumMeshes)`
13. **GPU data**（顶点/索引缓冲）

写出时使用两次序列化：第一次 dummy 计算大小，第二次回写正确偏移（`Serialize` 内部 `redo_offsets=True` 第二轮）。

### 2.2 `BoneInfo` (`unit.py:706-811`)

每个 LOD 对应一个 `BoneInfo`，描述本 LOD 用到的骨骼子集和 per-material 的 remap 表：

| 类型 | 字段 | 含义 |
|---|---|---|
| uint32 | `NumBones` | 真实骨骼数 |
| uint32 | `MatrixOffset` | 矩阵数组相对偏移 |
| uint32 | `RealIndicesOffset` | 真实索引数组相对偏移 |
| uint32 | `FakeIndicesOffset` | remap 索引数组相对偏移 |
| `StingrayMatrix4x4[NumBones]` | `Bones` | 每根骨骼的 4×4 矩阵（位于 `MatrixOffset`） |
| uint32[NumBones] | `RealIndices` | 真实骨骼 → 全局 transform 索引 |
| 复合结构 | `FakeIndices` | 见下 |

`FakeIndices` 子结构（位于 `FakeIndicesOffset`）：
```
uint32 NumRemaps
struct { uint32 offset; uint32 count; }[NumRemaps]   // 每个 material slot 的子表
uint32[count] ...                                     // 每个子表为 fake → real 的索引
```

工具中通过 `BoneInfo.SetRemap(remap_info, transform_info)` 重建该表：根据 material slot 提供的骨骼名列表 (`murmur32` 哈希) 找到全局 transform_index，若 LOD 中没有则会扩充 `RealIndices` 并追加 `None` 矩阵占位。

### 2.3 `StreamInfo` (`unit.py:813-858`)

描述一组顶点/索引缓冲及其布局，对应一个或多个 mesh 共享的缓冲：

| 类型 | 字段 |
|---|---|
| uint64 | `ComponentInfoID` |
| —— | 然后 `f.seek(start + 320)` 跳过组件描述符空间（最多 N 个 `StreamComponentInfo`，每个 23 字节，padding 到 320） |
| uint64 | `NumComponents` |
| uint64 | `VertexBufferID` |
| uint64 | `VertexBuffer_unk1` |
| uint32 | `NumVertices` |
| uint32 | `VertexStride` |
| uint64 | `VertexBuffer_unk2` |
| uint64 | `VertexBuffer_unk3` |
| uint64 | `IndexBufferID` |
| uint64 | `IndexBuffer_unk1` |
| uint32 | `NumIndices` |
| uint32 | `IndexBuffer_Type` | 0 = uint16 索引，1 = uint32 索引 |
| uint64 | `IndexBuffer_unk2` |
| uint64 | `IndexBuffer_unk3` |
| uint32 | `VertexBufferOffset` | 在 GPU 流中的字节偏移 |
| uint32 | `VertexBufferSize` |
| uint32 | `IndexBufferOffset` |
| uint32 | `IndexBufferSize` |
| bytes[16] | `UnkEndingBytes` |
| 写完后对齐到 16 |
| 在 `start + 8` 位置回填 | `StreamComponentInfo[NumComponents]` |

#### 2.3.1 `StreamComponentInfo` (`unit.py:1062-1146`)

| 类型 | 字段 |
|---|---|
| uint32 | `Type` |
| uint32 | `Format` |
| uint32 | `Index` (UV/BoneIndex 的多层索引) |
| uint64 | `Unknown` |

**Type 枚举**（`StreamComponentType`）：
| 值 | 名称 |
|---|---|
| 0 | POSITION |
| 1 | NORMAL |
| 2 | TANGENT |
| 3 | BITANGENT |
| 4 | UV |
| 5 | COLOR |
| 6 | BONE_INDEX |
| 7 | BONE_WEIGHT |

**Format 枚举（新版 unit, Version != 10800437）**：
| 值 | 名称 | Stride |
|---|---|---|
| 0 | FLOAT | 4 |
| 1 | VEC2_FLOAT | 8 |
| 2 | VEC3_FLOAT | 12 |
| 3 | VEC4_FLOAT | 16 |
| 4 | RGBA_R8G8B8A8 | 4 |
| 24 | VEC4_UINT32 | 16 |
| 28 | VEC4_UINT8 | 4 |
| 29 | VEC4_1010102 | 4 (10/10/10/2 packed) |
| 30 | UNK_NORMAL | 4 (uint32, octahedral packed) |
| 33 | VEC2_HALF | 4 |
| 35 | VEC4_HALF | 8 |

**Format 枚举（旧版 unit, Version == 10800437）**：枚举值偏移 -4（17 = UINT32, 21 = UINT8, 25 = VEC4_1010102, 26 = UNK_NORMAL, 28/29/30/31 = float16 系列）。

`VertexStride = ∑ Component.GetSize()`，整个顶点缓冲就是 `NumVertices × VertexStride` 的字节流。

#### 2.3.2 法线打包

`UNK_NORMAL` (Format 30) 使用八面体压缩 (octahedral) + 10/10 位编码：
```python
encode_packed_oct_norm(x,y,z): return int((x+1)*511.5) | (int((y+1)*511.5) << 10)
decode_packed_oct_norm(norm):  ...                # 见 unit.py:1303-1328
```
`VEC4_1010102` 通过 `TenBit(Un)signed` / `MakeTenBit(Un)signed` 编解码，第 4 通道占 2 位 (0..3)。

### 2.4 `MeshInfo` 与 `MeshSectionInfo` (`unit.py:860-916`)

每个可绘制 mesh 一个 `MeshInfo`（共 0x88+ 字节）：

| 类型 | 字段 |
|---|---|
| uint64 | `unk1` |
| bytes[32] | `unk2` |
| uint32 | `MeshID` |
| uint32 | `unk3` |
| uint32 | `TransformIndex` | 对应 `TransformInfo.TransformMatrices` 索引 |
| uint32 | `unk4` |
| int32 | `LodIndex` | -1 = 物理体/culling body, 0 = 主 LOD |
| uint32 | `StreamIndex` | 对应 `StreamInfoArray` 索引 |
| bytes[40] | `unk6` |
| uint32 | `NumMaterials` |
| uint32 | `MaterialOffset` |
| uint64 | `unk8` |
| uint32 | `NumSections` |
| uint32 | `SectionsOffset` |
| uint32[NumMaterials] | `MaterialIDs` | 32-bit 槽位 ID（材质短 ID） |
| `MeshSectionInfo[NumSections]` | `Sections` | |

`MeshSectionInfo` (24 字节)：
| uint32 | `MaterialIndex` |
| uint32 | `VertexOffset` | 在 stream 内的顶点起始 |
| uint32 | `NumVertices` |
| uint32 | `IndexOffset` | 在 stream 内的索引起始 (单位=索引数) |
| uint32 | `NumIndices` |
| uint32 | `GroupIndex` |

每个 Section 实际是一个三角组（指定材质 + 一段顶点/索引切片）。

### 2.5 变换信息 `TransformInfo` (`unit.py:1009-1031`, 只读)

```
uint32 NumTransforms
bytes[12] pad
StingrayLocalTransform Transforms[NumTransforms]   // 48 字节 = mat3 + vec3 + vec3 + float
StingrayMatrix4x4      TransformMatrices[NumTransforms]  // 64 字节
TransformEntry         TransformEntries[NumTransforms]   // uint16 Incriment + uint16 ParentBone
uint32                 NameHashes[NumTransforms]    // murmur32(boneName)
```

- `StingrayMatrix4x4` (16 float = 64 字节)：与 Autodesk Stingray SDK 一致。
- `StingrayMatrix3x3` (9 float = 36 字节)。
- `StingrayLocalTransform`：`rot(Matrix3x3) + pos(vec3) + scale(vec3) + dummy(float)`。

### 2.6 `CustomizationInfo` (`unit.py:1033-1060`, 只读)

是一段“通用用户数据”，实现里只读 4 个字符串：`BodyType / Slot / Weight / PieceType`，每个由 `uint32 length + bytes[length]` 组成，前后有 12 字节 padding（实际格式更复杂，注释里也明说是临时凑合的）。

### 2.7 `LightList` & `Light` (`unit.py:1243-1296`)

```
LightList:
  uint32 light_count
  uint32 unk0[3]
  Light  lights[light_count]

Light (固定大小)：
  uint32 name_hash         (murmur32(blender_light_name))
  uint32 bone_index        (在 TransformInfo.NameHashes 中的索引)
  float3 color
  float  intensity
  float  falloff_start
  float  falloff_end
  float  falloff_exp
  float  start_angle
  float  end_angle
  float  unk0
  float  shadow_bias       (默认 0.4)
  float  unk1[5]
  uint8  flags             (CAST_SHADOW=0x1, DISABLED=0x2, INDIRECT=0x4, VOLUMETRIC_FOG=0x10)
  uint8  pad[3]
  uint32 light_type        (0=OMNI, 1=SPOT, 2=BOX, 3=DIRECTIONAL)
  bytes  unk2[32]
```

Blender → 游戏映射（`AddLightOperator`）：
- `SpotLight`  → SPOT, `end_angle = spot_size, falloff_end = cutoff_distance`
- `PointLight` → OMNI, `falloff_end = cutoff_distance`
- `AreaLight`  → BOX，把 size 拆为 `±X/±Y/±Z` 各方向半径

### 2.8 复合体 `StingrayCompositeMesh` (`stingray/composite_unit.py`)

CompositeUnit 是把多个 Unit 共用同一组顶点/索引缓冲的“地图块/几何分组”容器：

```
CompositeMesh:
  uint64 unk1
  uint32 NumUnits
  uint32 StreamInfoOffset
  { uint64 UnitTypeHashes[i]; uint64 UnitHashes[i]; }[NumUnits]
  uint32 MeshInfoOffsets[NumUnits]
  CompositeMeshInfo  MeshInfos[NumUnits]      // 位于各自偏移
  bytes  Unreversed (填充到 StreamInfoOffset)
  // 对齐 16
  uint32 NumStreams
  uint32 StreamInfoOffsets[NumStreams]
  uint32 StreamInfoUnk[NumStreams]
  uint32 StreamInfoUnk2
  StreamInfo  Streams[NumStreams]
  GPU data (顶点/索引)

CompositeMeshInfo:
  uint32 MeshCount
  uint32 Meshes[MeshCount]                    // 每个 entry 的 MeshID 列表
  uint32 MeshInfoItemOffsets[MeshCount]
  CompositeMeshInfoItem  MeshInfoItems[MeshCount]

CompositeMeshInfoItem:
  uint32 MeshLayoutIdx        (StreamIndex)
  bytes[20] unk1
  uint32 NumMaterials
  uint32 MaterialsOffset
  uint64 unk2
  uint32 NumGroups
  uint32 GroupsOffset
  uint32 Materials[NumMaterials]  (位于 MaterialsOffset)
  MeshSectionInfo Groups[NumGroups]  (位于 GroupsOffset)
```

Unit 读取时若 `CompositeRef != 0`，会去 `CompositeRef` 资源里按 `NameHash` 找到对应 unit 的 `CompositeMeshInfo`，将 `MeshInfoArray` 的 `StreamIndex / Sections / MaterialIDs` 等用 CompositeMesh 中的值覆盖（这意味着原 Unit 文件中可能完全没有这些字段，需要从 Composite 中借来）。

### 2.9 骨骼资源 `StingrayBones` (`stingray/bones.py`)

类型 ID = `BoneID = 0x18DEAD01056B72E9`。

```
uint32 NumNames
uint32 NumLODLevels
float32 UnkArray1[NumLODLevels]
uint32  BoneHashes[NumNames]   // murmur32(name)
uint32  LODLevels[NumLODLevels] // 写入时全填 NumNames
bytes   stringz blob            // 以 \0 分隔的骨骼名串
```

加载后，工具会把 `(BoneHash → BoneName)` 注入到 `Global_BoneNames`，供之后所有 Unit 用。

### 2.10 `RawMeshClass` / `RawMaterialClass`（运行时中间结构）

`StingrayMeshFile.RawMeshes` 是 Blender 端解析出的 mesh 列表，每个元素：

```
RawMeshClass:
    MeshInfoIndex, MeshID
    VertexPositions[]    list[float3]
    VertexNormals[]
    VertexTangents[]
    VertexBiTangents[]
    VertexColors[]       list[float4]
    VertexWeights[]      list[float4]
    VertexBoneIndices[]  list[ list[uint4] ]    # 多层
    VertexUVs[]          list[ list[float2] ]   # 多层
    Indices[]            list[uint3 / int3]
    Materials[]          RawMaterialClass
    LodIndex, DEV_Use32BitIndices, DEV_BoneInfo, DEV_Transform
```

`RawMaterialClass`：
```
MatID       (str(int64) — material 资源 FileID)
ShortID     (uint32 — material slot 短 ID)
StartIndex, NumIndices
DEV_BoneInfoOverride
DEFAULT: MatID="StingrayDefaultMaterial", ShortID=155175220
```

写出 Unit 时，工具会按 `StreamInfoArray` 顺序对 `RawMeshes` 排序，再统一打包顶点+索引到 `GpuFile`，最后回填 `MeshSectionInfo.VertexOffset / IndexOffset / NumVertices / NumIndices`，并按 `Stream_Info.IndexBuffer_Type` 决定使用 16 还是 32 位索引（任一 mesh 设置 `DEV_Use32BitIndices=True` 即升级为 32 位）。

---

## 3. 贴图系统 (Texture)

类型 ID = `TexID = 0xCD4238C6A0C69E32`。
源文件：`stingray/texture.py:StingrayTexture`。

贴图横跨三个流：
- **toc 流** — 14 字节自定义头 + 15 个 mip 描述 + 148 字节 DDS 头
- **gpu 流** — 主分辨率以下若干 mip 的原始像素数据
- **stream 流**（可选）— 最高分辨率 mip 的原始像素数据（流式加载）

### 3.1 toc 部分

```
StingrayTexture toc layout:
  uint32  UnkID
  uint32  Unk1            // 写入时强制 0
  uint32  Unk2            // 写入时强制 0xFFFFFFFF
  StingrayMipmapInfo[15]   // 12 字节 × 15 = 180 字节
  bytes   ddsHeader[148]   // 标准 DDS magic+header + DX10 扩展头

StingrayMipmapInfo (12 字节):
  uint32 Start
  uint32 BytesLeft
  uint16 Height
  uint16 Width
```

15 槽位对应可能存在的多级 mipmap 信息（即便实际 mip 数小于 15 也固定写满）。

### 3.2 DDS 头要求 (`texture.py:69-98`)

工具要求贴图使用 **DX10 扩展头**（DDS_HEADER 末尾 84-87 字节必须是 ASCII `"DX10"`）。从中解析：

| 字段 | DDS 内偏移 | 含义 |
|---|---|---|
| `Height` | 12 | uint32 |
| `Width`  | 16 | uint32 |
| `NumMipMaps` | 28 | uint32 |
| `Format` | 128 | uint32 → 通过 `DXGI_FORMAT()` 查表得到名称 |
| `ArraySize` | 140 | uint32 |

`DXGI_FORMAT()` 内部映射了 132 个 DXGI 枚举值（标准 D3D11 列表，0..132），并暴露 `DXGI_FORMAT_SIZE(name)`：
- `BC1` / `BC4`             → 块大小 8 字节
- 其他 BC 系（BC2/3/5/6/7） → 块大小 16 字节
- 非 BC 格式                 → 抛 “currently unsupported”

### 3.3 GPU + Stream 像素体 (`texture.py:43-50`)

```
读取时：
  if len(Stream.Data) > 0: rawTex = Stream.Data        # 高清 mip 走 stream
  else:                    rawTex = Gpu.Data           # 否则 gpu_resources
写入时：
  Gpu.bytes(rawTex)                                    # 工具简化策略：永远写入 gpu，stream 留空
```

`ToDDS()` 直接拼接 `ddsHeader + rawTex` 即可获得标准 DDS 文件；`ToDDSArray()` 用于 `ArraySize > 1` 的 cubemap / texture array，把 raw 区按等长切片，每片前加修改过的 header (`array_size=1`)。

外部使用 `deps/texconv.exe` 转 PNG，方便 Blender 显示。

### 3.4 GPU mipmap 大小估算 (`texture.py:86-97`)

```
Stride = DXGI_FORMAT_SIZE(format) / 16
start_mip = max(1, NumMipMaps - 6)              # 通常 stream 携带前 6 级以外的小 mip
size = ∑ (CurrentWidth² * Stride),  CurrentWidth /= 2 each level (≥ 4)
```

这表明默认策略是：**前 6 级最大 mip 走 stream**，其余进 gpu_resources（也是反推 SDK 早期实现的依据）。

---

## 4. 材质 (Material)

类型 ID = `MaterialID = 0xEAC0B497876ADEDF`。
源文件：`stingray/material.py:StingrayMaterial`。

```
StingrayMaterial toc:
  bytes[12]  undat1
  uint32     EndOffset
  uint64     undat2
  uint64     ParentMaterialID    // 见 Global_MaterialParentIDs
  bytes[32]  undat3
  uint32     NumTextures
  bytes[36]  undat4
  uint32     NumVariables
  bytes[12]  undat5
  uint32     VariableDataSize
  bytes[12]  undat6
  uint32     TexUnks[NumTextures]
  uint64     TexIDs [NumTextures]     // 引用 TexID 资源的 FileID
  ShaderVariable[NumVariables]
  bytes      RemainingData            // 剩余字节按字面缓存
  // 之后回到 variableValueLocation, 按每个 variable.offset 写 (variable.klass+1) 个 float32
```

`ShaderVariable` (24 字节)：
```
uint32 klass           (0=Scalar, 1=Vector2, 2=Vector3, 3=Vector4, 12=Other)
uint32 elements
uint32 ID              (uint32 hash; 若在 hashlists/shadervariables.txt 中可解析为 name)
uint32 offset
uint32 elementStride
float32[klass + 1] values   (位于 variableValueLocation + offset)
```

材质模板：`Global_MaterialParentIDs` 把 `ParentMaterialID` 映射到 `basic/basic+/alphaclip/emissive/advanced/armorlut/translucent/original` 这 8 个模板，工具据此从 `materials/<name>.material` 拷贝模板并替换贴图。

---

## 5. 动画 (Animation)

类型 ID = `AnimationID = 0x931E336D7646CC26`。
源文件：`stingray/animation.py:StingrayAnimation`。

### 5.1 文件结构

```
uint32 unk
uint32 bone_count
float32 animation_length     // 秒
uint32 file_size             // 整文件字节数
uint32 hashes_count
uint32 hashes2_count
uint64 hashes [hashes_count]
uint64 hashes2[hashes2_count]
uint16 unk2

// 每根骨骼 3 个 bit 表示 (compress_position, compress_rotation, compress_scale)
// 总位数 = 3 * bone_count，向上取整到字节，再凑偶字节
// 注意：每个字节内部 bit 顺序是按位逆序写的（先翻转字节，再展开高位优先）
bytes  pack_flags[ ceil(3*bone_count/8) + (奇偶补齐) ]

// 每根骨骼的初始状态 (按 compress_* 选择)
for bone in range(bone_count):
  if compress_position: uint16[3]   (decompress: (v-32767) * 10/32767)
  else:                 float32[3]
  if compress_rotation: uint32      (最大分量索引 + 3*10bit ：见下)
  else:                 float32[4]
  if compress_scale   : uint16[3]
  else:                 float32[3]

// 每个 hash 对应的浮点 (用途未知)
float32 hashes_floats[hashes_count]

// 关键帧条目列表，直到读到 uint16(0x0003)
AnimationEntry  entries[...]
uint16 0x0003   // 终止符

// 文件结尾再写一次 size = file_size
uint32 size

// 然后整个块从 unk 到 entries+0x0003 又重复一遍 (历史遗留)
... 同上 ...
uint32 size_repeat
```

旋转压缩 (Smallest-Three 算法)：
```
largest_idx = index_of(max(rotation))
first/second/third = 其它 3 个分量
each → ((v / 0.75) * 512) + 512   ∈ [0, 1024)
打包: |  third<<22 | second<<12 | first<<2 | largest_idx
largest_val = sqrt(1 - first² - second² - third²)
```

### 5.2 `AnimationEntry` (关键帧)

每帧根据头字节高 2 位区分 type：
- type==0 (uncompressed special)：先读 `uint16 subtype`
  - subtype != 3：再读 `uint32 bone` + `float32 time(s) * 1000`
  - subtype == 2：触发音效（无 data2）
  - subtype == 4：未压缩位置 (`vec3 float`)
  - subtype == 5：未压缩四元数 (`vec4 float`)
  - subtype == 6：未压缩缩放
  - subtype == 3：终止符 (整文件结束)
- type==1：压缩缩放 — 12 位 bone + 20 位 time(ms) + `uint16[3]` 缩放
- type==2：压缩位置 — 同上 + `uint16[3]` 位置
- type==3：压缩旋转 — 同上 + `uint32` 旋转

帧头 bit layout（4 字节，type ≥ 1 时）：
```
bone   : data[0].high4 | data[1].low6        // 12 bits
time   : data[0].low4  | data[3]<<8 | data[2]  // 20 bits  (ms)
type   : data[1] >> 6                          // 2 bits
```

### 5.3 增量动画 (Additive)

任意 `initial_bone_states[i].scale[0] == 0` ⇒ `is_additive_animation = True`。
增量动画的初始 scale 写成 `[0,0,0]`，运行时把帧矩阵作为 pose 的 `matrix_basis` 直接乘。

---

## 6. 状态机 (StateMachine)

类型 ID = `StateMachineID = 0xA486D4045106165C`。
源文件：`stingray/state_machine.py`。
**实现注释明确说只解析 BlendMask / Ragdoll / Layer / State.animation_ids**，其余区段当作不透明数据保存。

```
StateMachine header (96 字节):
  uint32 unk
  uint32 layer_count
  uint32 layer_data_offset
  uint32 animation_events_count
  uint32 animation_events_offset
  uint32 animation_vars_count
  uint32 animation_vars_offset
  uint32 blend_mask_count
  uint32 blend_mask_offset
  uint32 unk_data_00_size / offset
  uint32 unk_data_01_size / offset
  uint32 unk_data_02_size / offset
  uint32 unk_data_03_size / offset
  uint32 ragdoll_count / offset
  bytes  pre_blend_mask_data (填到第一个非零 offset 处)

@ layer_data_offset:
  uint32 layer_count
  uint32 layer_offsets[layer_count]
  Layer:
    uint32 magic
    uint32 default_state
    uint32 num_states
    uint32 state_offsets[num_states]
    State[]:
      uint64 name
      uint32 state_type
      uint32 animation_count
      uint32 animation_offset
      bytes[88] (skipped)
      uint32 blend_mask_index    // 0xFFFFFFFF = 无 mask
      uint64 animation_ids[animation_count]    // 位于 animation_offset

@ blend_mask_offset:
  uint32 blend_mask_count
  uint32 blend_mask_offsets[blend_mask_count]
  BlendMask:
    uint32 bone_count
    float32 bone_weights[bone_count]

@ ragdoll_offset:
  RagdollItem[ragdoll_count]:
    uint32 bone_index
    float32 params[9]
    uint64 unk_hash
    uint32 unk_enum (=2)
    uint32 unk
```

写出时执行两次 `save()` 以更新最终偏移（与 Unit 类似的两遍法）。

---

## 7. 粒子 (Particle)

类型 ID = `ParticleID = 0xA8193123526FAD64`。
源文件：`stingray/particle.py`。

```
StingrayParticles:
  uint32 magic
  float32 minLifetime
  float32 maxLifetime
  uint32 unk1
  uint32 unk2
  uint32 numVariables
  uint32 numParticleSystems
  bytes[44] pad
  uint32 ParticleVariableHashes [numVariables]
  vec3   ParticleVariablePositions [numVariables]
  ParticleSystem  systems[numParticleSystems]

ParticleSystem:
  uint32 maxNumParticles
  uint32 numComponents
  uint32 unk2
  uint32 componentBitFlags[numComponents]
  bytes  pad (填到 64 - 4*numComponents)
  uint32 unk3                        // 0xFFFFFFFF ⇒ 非渲染粒子系统，跳过 ComponentList
  uint32 unk4
  bytes[8] pad
  uint32 unk5; bytes[4]
  uint32 unk6; bytes[4]
  uint32 type1, type2; bytes[4]
  ParticleRotation Rotation
  float32 unknown[11]
  uint32 unk7
  uint32 componentListOffset
  uint32 unk8
  uint32 componentListSize
  uint32 unk9, unk10
  uint32 offset3
  uint32 particleSystemSize          // 整 system 块字节数
  // @ start + componentListOffset
  ComponentList: byte[componentListSize - componentListOffset]
  // 末尾对齐到 start + particleSystemSize

ParticleRotation:
  float3 xRow (+ pad 4)
  float3 yRow (+ pad 4)
  float3 zRow (+ pad 4)
  uint8  unk[16]
```

---

## 8. 关键流程汇总

### 8.1 读取 Unit 的完整链路

```
LoadStingrayUnit(ID, TocData, GpuData, …):
  StingrayMeshFile.Serialize(toc, gpu, Global_TocManager)
    → 文件头偏移表
    → 通过 BonesRef 加载 StingrayBones（拿到 BoneName 字符串）
    → 解析 CustomizationInfo, LightList, TransformInfo
    → BoneInfo[]、StreamInfo[]、MeshInfo[]
    → 若 CompositeRef != 0: 切换到 Composite 的 StreamInfoArray + 各 MeshInfoItem
    → Materials slot 段
    → SerializeGpuData(gpu):
         InitRawMeshes() (从 MeshInfoArray 构造空白 RawMeshClass)
         CreateOrderedMeshList() (按 stream 内 VertexOffset/IndexOffset 排序)
         SerializeIndexBuffer + SerializeVertexBuffer (按 Components 解每个顶点)
```

### 8.2 写出 Unit

```
SaveStingrayUnit:
  StingrayMesh.Serialize(toc=writable, gpu=writable):
    构造每个 RawMesh 的 MeshSectionInfo（材质/分段）
    第 1 遍写：占位偏移 + 写完全部区块、得到真实偏移
    SerializeGpuData(gpu):
       SetupRawMeshComponents 决定 StreamInfo.Components
       VertexStride = ∑ Component.GetSize()
       依次写顶点、对齐 16、写索引，回填 Stream_Info 字段
    第 2 遍写（redo_offsets=True）：用真实偏移重新刷一次头部
```

### 8.3 贴图替换

```
1. 读 .dds → StingrayTexture.FromDDS(bytes)   # 拆 148 字节 header + raw
2. ParseDDSHeader 校验 DX10 + 取 W/H/Format/Mip/ArraySize
3. Serialize(Toc, Gpu, Stream=empty):
   - 写入 14 字节自定义头 + 15 个 StingrayMipmapInfo (全 0/默认)
   - 写入 148 字节 DDS header
   - rawTex 全量写入 Gpu 流 (stream 不写)
4. 把 toc/gpu/stream 作为新 TocEntry 加入 ActivePatch
```

---

## 附录 A: 文件位置速查

| 数据结构 | 源码位置 |
|---|---|
| MemoryStream / TenBit 编解码 | `utils/memoryStream.py` |
| Murmur32/64 | `utils/hashing.py` |
| 资源/类型 ID 常量 | `utils/constants.py` |
| DSAR / Bundle / Package 解压 | `utils/slim.py` |
| TocEntry / TocFileType / StreamToc / SearchToc / TocManager | `__init__.py:514-1030` |
| StingrayMeshFile + 子结构 (BoneInfo/StreamInfo/MeshInfo/…) | `stingray/unit.py:20-1297` |
| 顶点/法线编解码 LUT | `stingray/unit.py:1330-1566` |
| StingrayBones | `stingray/bones.py` |
| StingrayCompositeMesh | `stingray/composite_unit.py` |
| StingrayMaterial / ShaderVariable | `stingray/material.py` |
| StingrayTexture / DXGI 表 | `stingray/texture.py` |
| StingrayAnimation / Entry / Initial state | `stingray/animation.py` |
| StingrayStateMachine / Layer / BlendMask / Ragdoll | `stingray/state_machine.py` |
| StingrayParticles | `stingray/particle.py` |

## 附录 B: 已知魔数 / 哨兵值

| 数值 | 含义 |
|---|---|
| `0xF0000004` (4026531857) | StreamToc / Package 主文件头 magic |
| `0x52415344` ("DSAR", 1380012868) | DSAR 压缩包 magic |
| `155175220` | `RawMaterialClass.DefaultMaterialShortID`（StingrayDefaultMaterial 短 ID） |
| `"StingrayDefaultMaterial"` | 默认材质名 |
| `10800437` / `10800438` | 已知 Unit Version（影响顶点 Format 枚举偏移） |
| `0x03` (uint16) | StingrayAnimation 关键帧流终止符 |
| `0xFFFFFFFF` | State.blend_mask_index = 无 mask；ParticleSystem.unk3 = 非渲染粒子 |
| `0x18DEAD01056B72E9` | Bone 资源 TypeID (注意 `DEAD` 标识) |

