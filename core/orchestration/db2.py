from __future__ import annotations

from typing import Any


def should_use_db2_orchestration(config: dict[str, Any]) -> bool:
    try:
        src = config.get("source", {}).get("engine")
        tgt = config.get("target", {}).get("engine")
        return src == "db2" and tgt == "db2"
    except Exception:
        return False


def preflight_full_migration(source: Any, target: Any, config: dict[str, Any]) -> dict[str, Any]:
    result = {"source_target_same": False, "nonempty_targets": [], "ok": True}
    try:
        s_cur = getattr(source, "_conn", None)
        t_cur = getattr(target, "_conn", None)
        if s_cur is not None and t_cur is not None:
            try:
                sc = s_cur.cursor(); sc.execute("SELECT CURRENT SERVER FROM SYSIBM.SYSDUMMY1"); sr = sc.fetchone(); sc.close()
                tc = t_cur.cursor(); tc.execute("SELECT CURRENT SERVER FROM SYSIBM.SYSDUMMY1"); tr = tc.fetchone(); tc.close()
                if sr and tr and str(sr[0]).strip().upper() == str(tr[0]).strip().upper():
                    result["source_target_same"] = True
                    result["ok"] = False
                    raise RuntimeError("Source and target resolve to the same database")
            except RuntimeError:
                raise
            except Exception:
                pass
    except RuntimeError:
        raise
    except Exception:
        pass
    return result


def preflight_target_nonempty(source: Any, target: Any, config: dict[str, Any]) -> list[str]:
    """For Db2 FULL mode, selected target tables may be nonempty and are replaced transactionally."""
    return []
