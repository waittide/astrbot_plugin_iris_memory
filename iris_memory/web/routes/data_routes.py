"""
数据导入导出 API 路由

提供各核心数据模块的导入导出功能：
- L1 消息缓冲导出
- L2 记忆导出/导入
- L3 知识图谱导出/导入
- 画像导出/导入
- 全量导出/导入
"""

import json
from datetime import datetime

from quart import jsonify, request, Response
from ...core import get_component_manager, get_logger
from ...l1_buffer.buffer import L1Buffer
from ...l2_memory.io import MemoryExporter, MemoryImporter
from ...l2_memory.adapter import L2MemoryAdapter
from ...l3_kg.adapter import L3KGAdapter
from ...profile.storage import ProfileStorage

logger = get_logger("web.data")

PLUGIN_NAME = "astrbot_plugin_iris_memory"


async def export_l1_buffer():
    try:
        manager = get_component_manager()
        l1_buffer = manager.get_component("l1_buffer", L1Buffer)
        if not l1_buffer or not l1_buffer.is_available:
            return jsonify({"success": False, "error": "L1 缓冲不可用"}), 503

        group_id = request.args.get("group_id")
        export_data = l1_buffer.export_all(group_id=group_id or None)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{group_id}" if group_id else ""
        filename = f"iris_l1_buffer{suffix}_{timestamp}.json"

        logger.info("导出 L1 消息缓冲成功")
        return Response(
            json.dumps(export_data, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error(f"导出 L1 消息缓冲失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def import_l1_buffer():
    try:
        manager = get_component_manager()
        l1_buffer = manager.get_component("l1_buffer", L1Buffer)
        if not l1_buffer or not l1_buffer.is_available:
            return jsonify({"success": False, "error": "L1 缓冲不可用"}), 503

        content_type = request.content_type or ""
        skip_duplicates = True

        if "multipart/form-data" in content_type:
            files = await request.files
            if "file" not in files:
                return jsonify({"success": False, "error": "未找到上传文件"}), 400
            file_content = files["file"].read().decode("utf-8")
            import_data = json.loads(file_content)
        else:
            body = await request.get_json()
            if not body or "data" not in body:
                return jsonify({"success": False, "error": "请求体缺少 data 字段"}), 400
            import_data = body["data"]
            skip_duplicates = bool(body.get("skip_duplicates", True))

        try:
            stats = l1_buffer.import_from_data(
                import_data, skip_duplicates=skip_duplicates
            )
        except ValueError as ve:
            return jsonify({"success": False, "error": str(ve)}), 400

        logger.info(f"导入 L1 消息缓冲成功：{stats}")
        return jsonify({"success": True, "stats": stats})
    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"JSON 解析失败：{e}"}), 400
    except Exception as e:
        logger.error(f"导入 L1 消息缓冲失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def export_l2_memory():
    try:
        manager = get_component_manager()
        l2_adapter = manager.get_component("l2_memory", L2MemoryAdapter)

        if not l2_adapter or not l2_adapter.is_available:
            return jsonify({"success": False, "error": "L2 记忆库不可用"}), 503

        group_id = request.args.get("group_id")

        exporter = MemoryExporter(l2_adapter)
        export_data = exporter.export_to_json(await l2_adapter.get_all_entries())

        if group_id:
            data = json.loads(export_data)
            filtered = [
                e
                for e in data.get("entries", [])
                if e.get("metadata", {}).get("group_id") == group_id
            ]
            data["entries"] = filtered
            data["stats"] = {
                "total_count": len(filtered),
                "exported_count": len(filtered),
                "skipped_count": 0,
            }
            export_data = json.dumps(data, ensure_ascii=False, indent=2)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"iris_l2_memory_{timestamp}.json"

        logger.info("导出 L2 记忆成功")
        return Response(
            export_data,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as e:
        logger.error(f"导出 L2 记忆失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def import_l2_memory():
    try:
        manager = get_component_manager()
        l2_adapter = manager.get_component("l2_memory", L2MemoryAdapter)

        if not l2_adapter or not l2_adapter.is_available:
            return jsonify({"success": False, "error": "L2 记忆库不可用"}), 503

        content_type = request.content_type or ""
        skip_duplicates = True

        if "multipart/form-data" in content_type:
            files = await request.files
            if "file" not in files:
                return jsonify({"success": False, "error": "未找到上传文件"}), 400

            file = files["file"]
            file_content = file.read().decode("utf-8")
            import_data = json.loads(file_content)
        else:
            body = await request.get_json()
            if not body or "data" not in body:
                return jsonify({"success": False, "error": "请求体缺少 data 字段"}), 400
            import_data = body["data"]
            skip_duplicates = body.get("skip_duplicates", True)

        importer = MemoryImporter(l2_adapter)

        if isinstance(import_data, dict) and "entries" in import_data:
            entries_data = import_data["entries"]
        elif isinstance(import_data, list):
            entries_data = import_data
        else:
            return jsonify({"success": False, "error": "无法识别的导入数据格式"}), 400

        from ...l2_memory.models import MemoryEntry

        entries = [MemoryEntry.from_dict(entry_data) for entry_data in entries_data]
        stats = await importer.import_entries(entries, skip_duplicates=skip_duplicates)

        logger.info(f"导入 L2 记忆成功：{stats.imported_count} 条")
        return jsonify(
            {
                "success": True,
                "stats": {
                    "total_count": stats.total_count,
                    "imported_count": stats.imported_count,
                    "skipped_count": stats.skipped_count,
                    "error_count": stats.error_count,
                },
            }
        )

    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"JSON 解析失败：{e}"}), 400
    except Exception as e:
        logger.error(f"导入 L2 记忆失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def export_l3_kg():
    try:
        manager = get_component_manager()
        l3_adapter = manager.get_component("l3_kg", L3KGAdapter)

        if not l3_adapter or not l3_adapter.is_available:
            return jsonify({"success": False, "error": "L3 知识图谱不可用"}), 503

        export_data = await l3_adapter.export_all()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"iris_l3_kg_{timestamp}.json"

        logger.info("导出 L3 知识图谱成功")
        return Response(
            json.dumps(export_data, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as e:
        logger.error(f"导出 L3 知识图谱失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def import_l3_kg():
    try:
        manager = get_component_manager()
        l3_adapter = manager.get_component("l3_kg", L3KGAdapter)

        if not l3_adapter or not l3_adapter.is_available:
            return jsonify({"success": False, "error": "L3 知识图谱不可用"}), 503

        content_type = request.content_type or ""

        if "multipart/form-data" in content_type:
            files = await request.files
            if "file" not in files:
                return jsonify({"success": False, "error": "未找到上传文件"}), 400

            file = files["file"]
            file_content = file.read().decode("utf-8")
            import_data = json.loads(file_content)
            skip_duplicates = True
        else:
            body = await request.get_json()
            if not body or "data" not in body:
                return jsonify({"success": False, "error": "请求体缺少 data 字段"}), 400
            import_data = body["data"]
            skip_duplicates = body.get("skip_duplicates", True)

        stats = await l3_adapter.import_from_data(
            import_data, skip_duplicates=skip_duplicates
        )

        logger.info(
            f"导入 L3 知识图谱成功：{stats['imported_nodes']} 节点，{stats['imported_edges']} 边"
        )
        return jsonify({"success": True, "stats": stats})

    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"JSON 解析失败：{e}"}), 400
    except Exception as e:
        logger.error(f"导入 L3 知识图谱失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def export_profiles():
    try:
        manager = get_component_manager()
        profile_storage = manager.get_component("profile", ProfileStorage)

        if not profile_storage or not profile_storage.is_available:
            return jsonify({"success": False, "error": "画像系统不可用"}), 503

        export_data = await profile_storage.export_all()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"iris_profiles_{timestamp}.json"

        logger.info("导出画像成功")
        return Response(
            json.dumps(export_data, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as e:
        logger.error(f"导出画像失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def import_profiles():
    try:
        manager = get_component_manager()
        profile_storage = manager.get_component("profile", ProfileStorage)

        if not profile_storage or not profile_storage.is_available:
            return jsonify({"success": False, "error": "画像系统不可用"}), 503

        content_type = request.content_type or ""

        if "multipart/form-data" in content_type:
            files = await request.files
            if "file" not in files:
                return jsonify({"success": False, "error": "未找到上传文件"}), 400

            file = files["file"]
            file_content = file.read().decode("utf-8")
            import_data = json.loads(file_content)
            skip_duplicates = True
        else:
            body = await request.get_json()
            if not body or "data" not in body:
                return jsonify({"success": False, "error": "请求体缺少 data 字段"}), 400
            import_data = body["data"]
            skip_duplicates = body.get("skip_duplicates", True)

        stats = await profile_storage.import_from_data(
            import_data, skip_duplicates=skip_duplicates
        )

        logger.info(
            f"导入画像成功：{stats['imported_groups']} 群聊，{stats['imported_users']} 用户"
        )
        return jsonify({"success": True, "stats": stats})

    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"JSON 解析失败：{e}"}), 400
    except Exception as e:
        logger.error(f"导入画像失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def export_all():
    try:
        manager = get_component_manager()
        result = {
            "version": "1.2",
            "export_time": datetime.now().isoformat(),
            "l1_buffer": None,
            "l2_memory": None,
            "l3_kg": None,
            "profiles": None,
        }

        l1_buffer = manager.get_component("l1_buffer", L1Buffer)
        if l1_buffer and l1_buffer.is_available:
            try:
                result["l1_buffer"] = l1_buffer.export_all()
            except Exception as e:
                logger.warning(f"全量导出 L1 失败：{e}")
                result["l1_buffer"] = {"error": "内部错误，详见服务日志"}

        l2_adapter = manager.get_component("l2_memory", L2MemoryAdapter)
        if l2_adapter and l2_adapter.is_available:
            try:
                exporter = MemoryExporter(l2_adapter)
                all_entries = await l2_adapter.get_all_entries()
                export_json = exporter.export_to_json(all_entries)
                result["l2_memory"] = json.loads(export_json)
            except Exception as e:
                logger.warning(f"全量导出 L2 失败：{e}")
                result["l2_memory"] = {"error": "内部错误，详见服务日志"}

        l3_adapter = manager.get_component("l3_kg", L3KGAdapter)
        if l3_adapter and l3_adapter.is_available:
            try:
                result["l3_kg"] = await l3_adapter.export_all()
            except Exception as e:
                logger.warning(f"全量导出 L3 失败：{e}")
                result["l3_kg"] = {"error": "内部错误，详见服务日志"}

        profile_storage = manager.get_component("profile", ProfileStorage)
        if profile_storage and profile_storage.is_available:
            try:
                result["profiles"] = await profile_storage.export_all()
            except Exception as e:
                logger.warning(f"全量导出画像失败：{e}")
                result["profiles"] = {"error": "内部错误，详见服务日志"}

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"iris_full_backup_{timestamp}.json"

        logger.info("全量导出成功")
        return Response(
            json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as e:
        logger.error(f"全量导出失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def import_all():
    try:
        manager = get_component_manager()
        body = await request.get_json()

        if not body or "data" not in body:
            return jsonify({"success": False, "error": "请求体缺少 data 字段"}), 400

        import_data = body["data"]
        skip_duplicates = body.get("skip_duplicates", True)

        result = {
            "l1_buffer": None,
            "l2_memory": None,
            "l3_kg": None,
            "profiles": None,
        }

        l1_data = import_data.get("l1_buffer")
        if l1_data:
            l1_buffer = manager.get_component("l1_buffer", L1Buffer)
            if l1_buffer and l1_buffer.is_available:
                try:
                    result["l1_buffer"] = l1_buffer.import_from_data(
                        l1_data, skip_duplicates=skip_duplicates
                    )
                except Exception as e:
                    logger.warning(f"全量导入 L1 缓冲失败：{e}")
                    result["l1_buffer"] = {"error": "内部错误，详见服务日志"}
            else:
                result["l1_buffer"] = {"error": "L1 缓冲不可用"}

        l2_data = import_data.get("l2_memory")
        if l2_data:
            l2_adapter = manager.get_component("l2_memory", L2MemoryAdapter)
            if l2_adapter and l2_adapter.is_available:
                try:
                    importer = MemoryImporter(l2_adapter)
                    entries_data = (
                        l2_data.get("entries", [])
                        if isinstance(l2_data, dict)
                        else l2_data
                    )
                    from ...l2_memory.models import MemoryEntry

                    entries = [MemoryEntry.from_dict(e) for e in entries_data]
                    stats = await importer.import_entries(
                        entries, skip_duplicates=skip_duplicates
                    )
                    result["l2_memory"] = {
                        "total_count": stats.total_count,
                        "imported_count": stats.imported_count,
                        "skipped_count": stats.skipped_count,
                        "error_count": stats.error_count,
                    }
                except Exception as e:
                    logger.warning(f"全量导入 L2 记忆失败：{e}")
                    result["l2_memory"] = {"error": "内部错误，详见服务日志"}
            else:
                result["l2_memory"] = {"error": "L2 记忆库不可用"}

        l3_data = import_data.get("l3_kg")
        if l3_data:
            l3_adapter = manager.get_component("l3_kg", L3KGAdapter)
            if l3_adapter and l3_adapter.is_available:
                try:
                    stats = await l3_adapter.import_from_data(
                        l3_data, skip_duplicates=skip_duplicates
                    )
                    result["l3_kg"] = stats
                except Exception as e:
                    logger.warning(f"全量导入 L3 图谱失败：{e}")
                    result["l3_kg"] = {"error": "内部错误，详见服务日志"}
            else:
                result["l3_kg"] = {"error": "L3 知识图谱不可用"}

        profile_data = import_data.get("profiles")
        if profile_data:
            profile_storage = manager.get_component("profile", ProfileStorage)
            if profile_storage and profile_storage.is_available:
                try:
                    stats = await profile_storage.import_from_data(
                        profile_data, skip_duplicates=skip_duplicates
                    )
                    result["profiles"] = stats
                except Exception as e:
                    logger.warning(f"全量导入画像失败：{e}")
                    result["profiles"] = {"error": "内部错误，详见服务日志"}
            else:
                result["profiles"] = {"error": "画像系统不可用"}

        logger.info(f"全量导入完成：{result}")
        return jsonify({"success": True, "result": result})

    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"JSON 解析失败：{e}"}), 400
    except Exception as e:
        logger.error(f"全量导入失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


def register_data_routes(context) -> None:
    prefix = f"/{PLUGIN_NAME}/data"

    routes = [
        (f"{prefix}/l1/export", export_l1_buffer, ["GET"], "导出 L1 消息缓冲"),
        (f"{prefix}/l1/import", import_l1_buffer, ["POST"], "导入 L1 消息缓冲"),
        (f"{prefix}/l2/export", export_l2_memory, ["GET"], "导出 L2 记忆"),
        (f"{prefix}/l2/import", import_l2_memory, ["POST"], "导入 L2 记忆"),
        (f"{prefix}/l3/export", export_l3_kg, ["GET"], "导出 L3 知识图谱"),
        (f"{prefix}/l3/import", import_l3_kg, ["POST"], "导入 L3 知识图谱"),
        (f"{prefix}/profile/export", export_profiles, ["GET"], "导出画像"),
        (f"{prefix}/profile/import", import_profiles, ["POST"], "导入画像"),
        (f"{prefix}/all/export", export_all, ["GET"], "全量导出"),
        (f"{prefix}/all/import", import_all, ["POST"], "全量导入"),
    ]

    for route, handler, methods, desc in routes:
        context.register_web_api(route, handler, methods, desc)
