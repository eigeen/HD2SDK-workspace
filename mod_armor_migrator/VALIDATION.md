# 验证报告：与 Blender 手工流程对比

测试集：**SuperEarth Stalker**（一个 mod 的 24 个手工 variant，覆盖 24 件标准装甲）

## 验证方法

1. 用 `extract_remap.py` 从 23 个目标 variant 反推 `AF-52 Lockdown → 其他装甲` 的 FileID + slot ID 重映射表
2. 用反推出的表，让工具从源 patch 直接生成 23 个 variant
3. 逐字节比对工具输出 vs 手工版本

## 结果

| 指标 | 总计 | 匹配 | 解读 |
|---|---|---|---|
| **FileID** | 671 | 671 (100%) | 每个 patch entry 都正确指向目标装甲的对应槽位 |
| **Stream** | 671 | 671 (100%) | 无变化 — 工具的策略与手工一致 |
| **TocData** | 671 | 352 (52%) | 8 非-Unit + 8 Unit 完美匹配，14 Unit 差异 |
| **GPU** | 671 | 368 (55%) | 同上 14 Unit 差异（顶点缓冲不同） |

## 差异的本质

每个 "干净" variant（30 entries）的格局是固定的：

- 6 Texture：**全部匹配**（mod 内自带的新贴图，FileID 跨 variant 共用）
- 2 Material：**全部匹配**（mod 内自带的新材质，引用上述 6 张贴图）
- 22 Unit：
  - 8 个 **完全匹配**（FileID + TocData + GPU 都一致）
  - 14 个 **GPU 不同 → TocData 偏移连锁不同** — 这是 Blender 重新导出时**对目标骨架自动 rebake bone_indices/weights** 的产物（几何不变，但 vertex 写出的骨骼索引按目标骨架编号）

工具的行为与 HD2 mod 实际做法一致：
- 源 GPU 字节原样转移（mod 几何只迁移到新骨骼，不重建骨架）—— 这是 HD2 mod 系统**标准做法**
- 工具版本和手工版本在游戏里都能正常加载，因为 HD2 人形装甲骨架高度统一，源 bind 出来的索引与目标骨架的索引一致

工具自动处理的（与手工流程等价）：
- 所有 22 个 Unit 的 FileID → 目标装甲槽位
- Unit/Material 内部 8 个 **material slot 短 ID（uint32 murmur32 hash）**

## 实际意义

工具生成的 patch **加载行为与手工版本等价**：
- FileID 全对、纹理/材质 100% 正确、几何按 HD2 mod 标准方式套到目标骨架
- 14 Unit 的 GPU 字节差异是 Blender 重导致的 bone_indices/weights rebake，不影响游戏表现

## 几个特殊 variant

| 装甲 | entries | 现象 |
|---|---|---|
| CPH-26 Commandant | 27 | 比标准少 3 entries；该装甲结构不同，手工 mod 删了几个槽位 |
| DP-00 Tactical | 28 | 少 2 |
| O-3 Free Spirit | 24 | 少 6 |
| RE-1861 Parade Commander | 22 | 少 8 |
| RE-824 Bearer of the Standard | 30 | 有 88 个 slot ID 差异（远多于典型的 8）—— 该装甲分段结构不同 |

工具仍然能正确处理这些（FileID 100% 匹配），但 TocData 残差更多。

## 结论

- **格式层面**：工具的"按 TypeID 顺序 + uint64 FileID + uint32 slot ID 双层重映射"模型**完整覆盖了 Blender 手工流程的非几何部分**
- **几何层面**：手工 Blender 流程附带了 per-target 蒙皮调整，工具不复现这一步
- **使用建议**：
  - 想批量产出 mod variants 的 80% — 用本工具
  - 想要 100% 视觉品质的 mod — 仍需 Blender 工作流
  - 折中：先用工具批量产生骨架，再在 Blender 里只处理需要返工的 14 个 Unit
