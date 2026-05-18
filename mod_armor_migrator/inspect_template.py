"""Audit the built-in empty mesh for embedded dependencies."""
from __future__ import annotations

import struct

from .padding import audit_empty_unit, builtin_template, sanitize_empty_unit
from .archive import StreamToc


def main():
    t = builtin_template()
    raw_audit = audit_empty_unit(t.toc_data)
    clean_toc = sanitize_empty_unit(t.toc_data)
    clean_audit = audit_empty_unit(clean_toc)
    print(f"built-in empty mesh — toc={len(t.toc_data)} B, gpu={len(t.gpu_data)} B, "
          f"stream={len(t.stream_data)} B")
    print(f"  source FileID (informational): 0x{t.source_file_id:016x}\n")
    print("Audit summary:")
    print(f"  raw external refs:       {len(raw_audit.external_refs)}")
    print(f"  raw draw indices:        {raw_audit.num_indices}")
    print(f"  raw section indices:     {raw_audit.section_indices}")
    print(f"  sanitized external refs: {len(clean_audit.external_refs)}")
    print(f"  sanitized draw indices:  {clean_audit.num_indices}")
    print(f"  sanitized section idx:   {clean_audit.section_indices}")
    print()

    # 5 header refs
    refs = struct.unpack_from("<QQQQQ", t.toc_data, 0)
    names = ["UnkRef1", "BonesRef", "CompositeRef", "UnkRef2", "StateMachineRef"]
    print("Unit header references (offset 0..40):")
    for n, r in zip(names, refs):
        flag = "ZERO ok" if r == 0 else f"NON-ZERO ← references 0x{r:016x}"
        print(f"  {n:18s}  0x{r:016x}  {flag}")
    print()

    # All header offsets
    print("Offset fields:")
    (
        lod_off, transform_off, light_off, prelight_off, wwise_off,
    ) = struct.unpack_from("<IIIII", t.toc_data, 0x30)
    (
        cust_off, unkh_off, conn_off, boneinfo_off, stream_off,
        ending_off, mesh_off,
    ) = struct.unpack_from("<IIIIIII", t.toc_data, 0x4C)
    materials_off, = struct.unpack_from("<I", t.toc_data, 0x70)
    version, = struct.unpack_from("<I", t.toc_data, 0x2C)
    print(f"  Version                 = {version}")
    print(f"  UnreversedLODGroupListDataOffset = 0x{lod_off:x}")
    print(f"  TransformInfoOffset     = 0x{transform_off:x}")
    print(f"  LightListOffset         = 0x{light_off:x}")
    print(f"  UnkPreLightListOffset   = 0x{prelight_off:x}")
    print(f"  WwiseCallbackOffset     = 0x{wwise_off:x}")
    print(f"  CustomizationInfoOffset = 0x{cust_off:x}")
    print(f"  UnkHeaderOffset1        = 0x{unkh_off:x}")
    print(f"  ConnectingBoneHashOffset= 0x{conn_off:x}")
    print(f"  BoneInfoOffset          = 0x{boneinfo_off:x}")
    print(f"  StreamInfoOffset        = 0x{stream_off:x}")
    print(f"  EndingOffset            = 0x{ending_off:x}")
    print(f"  MeshInfoOffset          = 0x{mesh_off:x}")
    print(f"  MaterialsOffset         = 0x{materials_off:x}")
    print()

    # Materials slot
    if materials_off and materials_off + 4 <= len(t.toc_data):
        num_mats, = struct.unpack_from("<I", t.toc_data, materials_off)
        print(f"Materials slot @0x{materials_off:x}: NumMaterials={num_mats}")
        if num_mats > 0:
            section_ids = struct.unpack_from(
                f"<{num_mats}I", t.toc_data, materials_off + 4
            )
            mat_ids = struct.unpack_from(
                f"<{num_mats}Q", t.toc_data, materials_off + 4 + 4 * num_mats
            )
            print(f"  SectionsIDs (uint32 slot hashes): "
                  f"{[hex(s) for s in section_ids]}")
            print(f"  MaterialIDs (uint64 FileIDs):     "
                  f"{[hex(m) for m in mat_ids]}")
            for m in mat_ids:
                if m != 0:
                    print(f"    !! MaterialID 0x{m:016x} references an external "
                          f"Material resource — will fail to load on any target "
                          f"that doesn't already have it.")
    print()

    # MeshInfo
    if mesh_off and mesh_off + 4 <= len(t.toc_data):
        # MeshInfo array starts with NumMeshes... actually MeshInfoOffset points
        # to the start of the array; from docs section 2.1, the array length is
        # stored in EndingBytes (uint64 = NumMeshes), which sits at EndingOffset.
        if ending_off:
            num_meshes_low, = struct.unpack_from("<I", t.toc_data, ending_off)
            print(f"NumMeshes (from EndingOffset 0x{ending_off:x}): {num_meshes_low}")

    # BoneInfoOffset — if non-zero, the mesh has skinning data
    if boneinfo_off and boneinfo_off + 16 <= len(t.toc_data):
        num_bones, mat_off, real_off, fake_off = struct.unpack_from(
            "<IIII", t.toc_data, boneinfo_off
        )
        print(f"BoneInfo @0x{boneinfo_off:x}: NumBones={num_bones}, "
              f"MatrixOffset=0x{mat_off:x}, RealIndicesOffset=0x{real_off:x}, "
              f"FakeIndicesOffset=0x{fake_off:x}")

    # StreamInfo[0]
    if stream_off:
        f = t.toc_data
        # NumStreams at stream_off, then offsets at stream_off + 4 + 4*i
        num_streams, = struct.unpack_from("<I", f, stream_off)
        print(f"\nStreamInfo @0x{stream_off:x}: NumStreams={num_streams}")
        for i in range(num_streams):
            si_off, = struct.unpack_from("<I", f, stream_off + 4 + 4 * i)
            base = stream_off + si_off if si_off < 0x1000 else si_off
            # StreamInfo struct: see docs §2.3
            # uint64 ComponentInfoID; then f.seek(start+320); ...
            num_vertices, vertex_stride = struct.unpack_from("<II", f, base + 344)
            num_indices, idx_type = struct.unpack_from("<II", f, base + 384)
            print(f"  stream #{i}: vertices={num_vertices}, "
                  f"stride={vertex_stride}, indices={num_indices}, "
                  f"index_type={'uint32' if idx_type else 'uint16'}")

    print(f"\nGPU buffer size: {len(t.gpu_data)} bytes "
          f"({len(t.gpu_data)/1024:.1f} KB)")
    if len(t.gpu_data) > 4096:
        print(f"  !! large for a single-point mesh — likely contains "
              f"per-bone matrices or padding from the SDK export.")

    # Re-parse the embedded patch to confirm there's only one entry.
    from . import _builtin_empty_mesh as m
    toc = StreamToc.from_buffers(m.TOC_DATA, m.GPU_DATA, m.STREAM_DATA)
    print(f"\nEmbedded patch contains {len(toc.entries)} entry(ies):")
    for e in toc.entries:
        from .constants import TYPE_NAMES
        print(f"  TypeID={TYPE_NAMES.get(e.type_id, hex(e.type_id))}  "
              f"FileID=0x{e.file_id:016x}  toc={len(e.toc_data)}  "
              f"gpu={len(e.gpu_data)}  stream={len(e.stream_data)}")


if __name__ == "__main__":
    main()
