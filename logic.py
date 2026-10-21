import copy
import datetime
import json
import os
import re
import subprocess
import time
import traceback
from xml.sax.saxutils import escape
import requests
from flask import Response, jsonify, render_template, request, send_from_directory, stream_with_context
from plugin import F, PluginModuleBase
from .setup import P

logger = P.logger
ModelSetting = P.ModelSetting
SystemModelSetting = F.SystemModelSetting

DEFAULT_CUSTOM_CHANNELS = [
    {
        "id": "yonhapnewstv",
        "name": "연합뉴스TV",
        "group": "기타",
        "type": "direct",
        "source_type": "tving",
        "source_val": "C01723",
        "audio_extract": True,
        "logo": "yonhapnewstv.png",
        "epg": "815508.naver"
    },
    {
        "id": "ytn_tv",
        "name": "YTN",
        "group": "기타",
        "type": "direct",
        "source_type": "tving",
        "source_val": "C00593",
        "audio_extract": True,
        "logo": "ytn.png",
        "epg": "814912.naver"
    }
]

_ENC_KEY = b"ff_kradio_2026_safe_key"

def _decrypt_payload(enc_str):
    try:
        import zlib, base64
        xored = base64.b85decode(enc_str.strip().encode("ascii"))
        comp = bytes(b ^ _ENC_KEY[i % len(_ENC_KEY)] for i, b in enumerate(xored))
        raw = zlib.decompress(comp)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.error(f"[ff_kradio] 복호화 실패: {e}")
        return None

class Logic(PluginModuleBase):
    db_default = {
        "upstream_url": "",
        "ddns_url": "",
        "apikey": "",
        "custom_channels_json": json.dumps(DEFAULT_CUSTOM_CHANNELS, ensure_ascii=False),
        "use_raw_override": "False",
        "raw_json_override": ""
    }

    def __init__(self, PM):
        super().__init__(PM, None)
        self.name = "setting"
        self.cache_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_path = os.path.join(self.cache_dir, "upstream_cache.json")
        self.xmltv_upstream_path = os.path.join(self.cache_dir, "upstream_xmltv.xml")
        self.xmltv_cache_path = os.path.join(self.cache_dir, "xmltv_merged.xml")
        self.logo_dir = os.path.join(self.cache_dir, "logo")
        os.makedirs(self.logo_dir, exist_ok=True)

    def get_setting(self, key):
        val = ModelSetting.get(key)
        if val is not None and val != "":
            return val
        if key == "ddns_url":
            try:
                sys_ddns = SystemModelSetting.get("ddns")
                if sys_ddns:
                    return sys_ddns.rstrip("/")
            except Exception:
                pass
            try:
                from flask import has_request_context, request
                if has_request_context():
                    return request.host_url.rstrip("/")
            except Exception:
                pass
            return ""
        if key == "apikey":
            try:
                sys_key = SystemModelSetting.get("apikey")
                if sys_key:
                    return sys_key
            except Exception:
                pass
            return ""
        return self.db_default.get(key, "")

    def plugin_load(self):
        try:
            for k, v in self.db_default.items():
                if not ModelSetting.has_key(k):
                    ModelSetting.set(k, v)
            if not ModelSetting.get("ddns_url"):
                ModelSetting.set("ddns_url", self.get_setting("ddns_url"))
            if not ModelSetting.get("apikey"):
                ModelSetting.set("apikey", self.get_setting("apikey"))

            # 기본 제공 로고 확인
            pass

            # 기존 설정에 위키미디어 등 깨지는 URL이 있거나 EPG가 비어있으면 자동 마이그레이션
            channels = self.get_custom_channels()
            changed = False
            for ch in channels:
                logo = ch.get("logo", "")
                if "wikimedia" in logo or not logo:
                    ch["logo"] = "yonhapnewstv.png"
                    changed = True
                if ch.get("id") == "yonhapnewstv" and not ch.get("epg"):
                    ch["epg"] = "815508.naver"
                    changed = True
            if changed:
                ModelSetting.set("custom_channels_json", json.dumps(channels, ensure_ascii=False))

            self.fetch_upstream(force=False)
            logger.info("[ff_kradio] 플러그인이 성공적으로 로드되었습니다.")
        except Exception as e:
            logger.error(f"[ff_kradio] plugin_load 예외: {e}")

    def get_base_channels(self):
        enc_path = os.path.join(self.cache_dir, "default_channels.enc")
        if os.path.exists(enc_path):
            try:
                with open(enc_path, "r", encoding="ascii") as f:
                    data = _decrypt_payload(f.read())
                    if data and data.get("channels"):
                        return data.get("channels", [])
            except Exception as e:
                logger.error(f"[ff_kradio] get_base_channels enc 에러: {e}")

        default_path = os.path.join(self.cache_dir, "default_channels.json")
        if os.path.exists(default_path):
            try:
                with open(default_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("channels", [])
            except Exception as e:
                logger.error(f"[ff_kradio] get_base_channels json 에러: {e}")
        return []

    def get_disabled_base_channels(self):
        raw = ModelSetting.get("disabled_base_channels") or "[]"
        try:
            return json.loads(raw)
        except Exception:
            return []

    def get_custom_channels(self):
        try:
            raw = ModelSetting.get("custom_channels_json")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.error(f"[ff_kradio] get_custom_channels 파싱 예외: {e}")
        return copy.deepcopy(DEFAULT_CUSTOM_CHANNELS)

    def fetch_upstream(self, force=False):
        upstream_url = (self.get_setting("upstream_url") or "").strip()
        if upstream_url:
            if not force and os.path.exists(self.cache_path):
                try:
                    with open(self.cache_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data.get("channels"):
                            return data
                except Exception as e:
                    logger.warning(f"[ff_kradio] 캐시 로드 실패: {e}")

            try:
                res = requests.get(upstream_url, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    with open(self.cache_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    return data
            except Exception as e:
                logger.error(f"[ff_kradio] upstream 다운로드 실패: {e}")

        # upstream_url이 지정되지 않았거나 다운로드 실패 시 번들된 기본 채널 목록 로드
        enc_path = os.path.join(self.cache_dir, "default_channels.enc")
        if os.path.exists(enc_path):
            try:
                with open(enc_path, "r", encoding="ascii") as f:
                    data = _decrypt_payload(f.read())
                    if data and data.get("channels"):
                        return data
            except Exception as e:
                logger.warning(f"[ff_kradio] default_channels.enc 로드 실패: {e}")

        default_json_path = os.path.join(self.cache_dir, "default_channels.json")
        if os.path.exists(default_json_path):
            try:
                with open(default_json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("channels"):
                        return data
            except Exception as e:
                logger.warning(f"[ff_kradio] default_channels.json 로드 실패: {e}")

        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"version": 2, "channels": []}

    def fetch_naver_epg(self, service_id):
        """네이버 모바일 방송 편성표에서 어제, 오늘, 내일 일정을 가져옵니다."""
        now = datetime.datetime.now()
        dates = [
            (now - datetime.timedelta(days=1)).strftime('%Y%m%d'),
            now.strftime('%Y%m%d'),
            (now + datetime.timedelta(days=1)).strftime('%Y%m%d')
        ]
        progs = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        for d in dates:
            try:
                url = f"https://m.search.naver.com/p/csearch/content/nqapirender.nhn?key=SingleChannelDailySchedule&where=m&pkid=66&u1={service_id}&u2={d}"
                res = requests.get(url, headers=headers, timeout=5)
                if res.status_code != 200:
                    continue
                data = res.json()
                html = "".join(data.get("dataHtml", []))
                chunks = html.split('<li class="list')
                for c in chunks[1:]:
                    t_match = re.search(r'<div class="time">([^<]+)</div>', c)
                    pr_match = re.search(r'<div class="pr_title">(.*?)</div>', c, re.DOTALL)
                    if t_match and pr_match:
                        time_hm = t_match.group(1).strip()
                        title = re.sub(r'<[^>]+>', '', pr_match.group(1)).strip()
                        if ":" in time_hm:
                            hh, mm = time_hm.split(":", 1)
                            try:
                                dt = datetime.datetime.strptime(f"{d}{hh.zfill(2)}{mm.zfill(2)}00", "%Y%m%d%H%M%S")
                                progs.append({"dt": dt, "title": title})
                            except Exception:
                                pass
            except Exception as e:
                logger.warning(f"[ff_kradio] Naver EPG 가져오기 오류 ({service_id}, {d}): {e}")

        progs.sort(key=lambda x: x["dt"])
        return progs

    def get_merged_xmltv(self, force=False):
        """기본 upstream XMLTV와 커스텀 채널(네이버 EPG)을 합친 XML을 반환합니다."""
        now_ts = time.time()
        if not force and os.path.exists(self.xmltv_cache_path) and (now_ts - os.path.getmtime(self.xmltv_cache_path) < 1800):
            try:
                with open(self.xmltv_cache_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"[ff_kradio] xmltv 캐시 읽기 실패: {e}")

        upstream_content = ""
        if os.path.exists(self.xmltv_upstream_path) and (now_ts - os.path.getmtime(self.xmltv_upstream_path) < 3600):
            try:
                with open(self.xmltv_upstream_path, "r", encoding="utf-8") as f:
                    upstream_content = f.read()
            except Exception:
                pass

        if not upstream_content:
            upstream_epg_url = (self.get_setting("upstream_epg_url") or "").strip()
            try:
                r = requests.get(upstream_epg_url, timeout=10)
                if r.status_code == 200:
                    upstream_content = r.text
                    with open(self.xmltv_upstream_path, "w", encoding="utf-8") as f:
                        f.write(upstream_content)
            except Exception as e:
                logger.error(f"[ff_kradio] upstream xmltv 다운로드 실패: {e}")

        if not upstream_content:
            upstream_content = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n<tv generator-info-name="ff_kradio">\n</tv>'

        custom_channels = self.get_custom_channels()
        extra_channels_xml = []
        extra_programmes_xml = []

        for ch in custom_channels:
            epg_id = (ch.get("epg") or "").strip()
            ch_name = ch.get("name") or ch.get("id")
            if not epg_id:
                continue

            naver_service_id = None
            if epg_id.endswith(".naver"):
                naver_service_id = epg_id[:-6]
            elif epg_id.isdigit():
                naver_service_id = epg_id
                epg_id = f"{naver_service_id}.naver"

            if naver_service_id:
                progs = self.fetch_naver_epg(naver_service_id)
                if progs:
                    extra_channels_xml.append(f'  <channel id="{escape(epg_id)}">\n    <display-name>{escape(ch_name)}</display-name>\n  </channel>')
                    for i in range(len(progs)):
                        p = progs[i]
                        start_str = p["dt"].strftime("%Y%m%d%H%M%S +0900")
                        if i + 1 < len(progs):
                            stop_str = progs[i + 1]["dt"].strftime("%Y%m%d%H%M%S +0900")
                        else:
                            stop_str = (p["dt"] + datetime.timedelta(hours=1)).strftime("%Y%m%d%H%M%S +0900")
                        title_esc = escape(p["title"])
                        extra_programmes_xml.append(f'  <programme start="{start_str}" stop="{stop_str}" channel="{escape(epg_id)}">\n    <title lang="ko">{title_esc}</title>\n  </programme>')

        if extra_channels_xml or extra_programmes_xml:
            injection = "\n" + "\n".join(extra_channels_xml) + "\n" + "\n".join(extra_programmes_xml) + "\n"
            if "</tv>" in upstream_content:
                pos = upstream_content.rfind("</tv>")
                merged_xml = upstream_content[:pos] + injection + upstream_content[pos:]
            else:
                merged_xml = upstream_content + injection + "</tv>"
        else:
            merged_xml = upstream_content

        try:
            with open(self.xmltv_cache_path, "w", encoding="utf-8") as f:
                f.write(merged_xml)
        except Exception as e:
            logger.warning(f"[ff_kradio] xmltv 캐시 쓰기 실패: {e}")

        return merged_xml

    def build_channels_json(self):
        if ModelSetting.get_bool("use_raw_override"):
            raw = ModelSetting.get("raw_json_override")
            if raw and raw.strip():
                try:
                    return json.loads(raw)
                except Exception as e:
                    logger.error(f"[ff_kradio] raw_json_override 파싱 실패: {e}")

        ddns = (self.get_setting("ddns_url") or "").rstrip("/")
        apikey = self.get_setting("apikey") or ""

        # 1. Base channels (filter disabled ones)
        all_base = self.get_base_channels()
        disabled_set = set(self.get_disabled_base_channels())
        base_channels = []
        for ch in all_base:
            if ch.get("id") in disabled_set:
                continue
            # Keep original logo string for base channels (KRadio app has them built into APK assets)
            item = copy.deepcopy(ch)
            base_channels.append(item)

        # 2. Custom channels
        custom_channels = self.get_custom_channels()
        for ch in custom_channels:
            ch_id = ch.get("id")
            if not ch_id:
                continue

            epg_val = (ch.get("epg") or "").strip()
            if not epg_val and ch_id == "yonhapnewstv":
                epg_val = "815508.naver"
            elif not epg_val and ch_id == "ytn_tv":
                epg_val = "814912.naver"

            audio_extract = ch.get("audio_extract", True)
            source_val = (ch.get("source_val") or "").strip()
            source_type = ch.get("source_type", "direct")

            if audio_extract:
                stream_url = f"{ddns}/ff_kradio/audio/{ch_id}"
            else:
                if source_val.startswith("http://") or source_val.startswith("https://"):
                    stream_url = source_val
                elif source_val.startswith("C0") or source_type == "tving":
                    stream_url = f"http://127.0.0.1:8282/alive/api/url.m3u8?m=url&s=tving&i={source_val}&apikey={apikey}"
                else:
                    stream_url = source_val

            raw_logo = (ch.get("logo") or "").strip()
            if not raw_logo:
                logo_url = f"{ddns}/ff_kradio/logo/{ch_id}.png"
            elif raw_logo.startswith("http://") or raw_logo.startswith("https://"):
                logo_url = raw_logo
            else:
                logo_url = f"{ddns}/ff_kradio/logo/{raw_logo}"

            entry = {
                "id": ch_id,
                "name": ch.get("name", "이름없음"),
                "group": ch.get("group", "기타"),
                "type": "direct",
                "epg": epg_val,
                "logo": logo_url,
                "url": stream_url
            }
            base_channels.append(entry)

        data = {
            "version": 2,
            "latestVersionCode": 22,
            "latestVersionName": "1.10.1",
            "epgUrl": f"{ddns}/ff_kradio/xmltv.xml",
            "channels": base_channels
        }
        return data

    def process_menu(self, sub, req):
        ddns = (self.get_setting("ddns_url") or "").rstrip("/")
        channels_url = f"{ddns}/ff_kradio/channels.json"
        epg_url = f"{ddns}/ff_kradio/xmltv.xml"

        arg = {
            "upstream_url": self.get_setting("upstream_url"),
            "ddns_url": self.get_setting("ddns_url"),
            "apikey": self.get_setting("apikey"),
            "channels_url": channels_url,
            "epg_url": epg_url,
            "base_channels": self.get_base_channels(),
            "disabled_base": self.get_disabled_base_channels(),
            "custom_channels": self.get_custom_channels(),
            "use_raw_override": ModelSetting.get_bool("use_raw_override"),
            "raw_json_override": ModelSetting.get("raw_json_override") or "",
        }

        if sub == "setting":
            return render_template("ff_kradio_setting.html", arg=arg)
        elif sub == "json_view":
            current_json = json.dumps(self.build_channels_json(), ensure_ascii=False, indent=2)
            arg["current_json"] = current_json
            return render_template("ff_kradio_json_view.html", arg=arg)
        return render_template("ff_kradio_setting.html", arg=arg)

    def process_ajax(self, sub, req):
        try:
            if sub == "save_setting":
                ModelSetting.set("upstream_url", req.form.get("upstream_url", "").strip())
                ModelSetting.set("ddns_url", req.form.get("ddns_url", "").strip())
                ModelSetting.set("apikey", req.form.get("apikey", "").strip())
                return jsonify({"ret": "success", "message": "설정이 저장되었습니다."})

            elif sub == "toggle_base_channel":
                ch_id = req.form.get("id", "").strip()
                enabled = str(req.form.get("enabled", "true")).lower() in ("true", "1", "on")
                disabled = set(self.get_disabled_base_channels())
                if enabled:
                    disabled.discard(ch_id)
                else:
                    disabled.add(ch_id)
                ModelSetting.set("disabled_base_channels", json.dumps(list(disabled)))
                return jsonify({"ret": "success"})

            elif sub == "sync_base_channels":
                sync_url = "https://raw.githubusercontent.com/fromhd/ff_kradio/main/default_channels.enc"
                try:
                    res = requests.get(sync_url, timeout=10)
                    if res.status_code == 200:
                        enc_text = res.text
                        data = _decrypt_payload(enc_text)
                        if data and data.get("channels") and len(data["channels"]) > 0:
                            enc_path = os.path.join(self.cache_dir, "default_channels.enc")
                            with open(enc_path, "w", encoding="ascii") as f:
                                f.write(enc_text.strip())
                            return jsonify({"ret": "success", "message": f"공식 기본 채널 {len(data['channels'])}개가 최신으로 동기화되었습니다."})
                except Exception as e:
                    logger.error(f"[ff_kradio] sync_base_channels 실패: {e}")
                    return jsonify({"ret": "error", "message": f"동기화 실패: {e}"})
                return jsonify({"ret": "error", "message": "동기화할 채널 데이터를 가져오지 못했습니다."})

            elif sub == "save_channel":
                orig_id = req.form.get("orig_id", "").strip()
                ch_id = req.form.get("id", "").strip()
                name = req.form.get("name", "").strip()
                group = req.form.get("group", "기타").strip()
                source_type = req.form.get("source_type", "tving").strip()
                source_val = req.form.get("source_val", "").strip()
                audio_extract = str(req.form.get("audio_extract", "false")).lower() in ("true", "1", "on")
                insert_after = req.form.get("insert_after", "").strip()
                logo = req.form.get("logo", "").strip()
                epg = req.form.get("epg", "").strip()

                if not ch_id or not name:
                    return jsonify({"ret": "error", "message": "채널 ID와 채널명을 입력해주세요."})

                channels = self.get_custom_channels()
                updated = False
                target_key = orig_id if orig_id else ch_id
                for ch in channels:
                    if ch.get("id") == target_key:
                        ch["id"] = ch_id
                        ch["name"] = name
                        ch["group"] = group
                        ch["source_type"] = source_type
                        ch["source_val"] = source_val
                        ch["audio_extract"] = audio_extract
                        ch["insert_after"] = insert_after
                        ch["logo"] = logo
                        ch["epg"] = epg
                        updated = True
                        break

                if not updated:
                    channels.append({
                        "id": ch_id,
                        "name": name,
                        "group": group,
                        "type": "direct",
                        "source_type": source_type,
                        "source_val": source_val,
                        "audio_extract": audio_extract,
                        "insert_after": insert_after,
                        "logo": logo,
                        "epg": epg
                    })

                ModelSetting.set("custom_channels_json", json.dumps(channels, ensure_ascii=False))
                # 채널 변경 시 EPG 캐시 무효화
                if os.path.exists(self.xmltv_cache_path):
                    try:
                        os.remove(self.xmltv_cache_path)
                    except Exception:
                        pass
                return jsonify({"ret": "success", "message": "채널이 저장되었습니다."})

            elif sub == "delete_channel":
                ch_id = req.form.get("id", "").strip()
                channels = self.get_custom_channels()
                channels = [c for c in channels if c.get("id") != ch_id]
                ModelSetting.set("custom_channels_json", json.dumps(channels, ensure_ascii=False))
                if os.path.exists(self.xmltv_cache_path):
                    try:
                        os.remove(self.xmltv_cache_path)
                    except Exception:
                        pass
                return jsonify({"ret": "success", "message": "채널이 삭제되었습니다."})

            elif sub == "refresh_upstream":
                data = self.fetch_upstream(force=True)
                return jsonify({"ret": "success", "message": f"기본 채널 갱신 완료 ({len(data.get('channels', []))}개)"})

            elif sub == "refresh_epg":
                self.get_merged_xmltv(force=True)
                return jsonify({"ret": "success", "message": "EPG 편성표가 즉시 갱신되었습니다."})

            elif sub == "save_raw_json":
                use_raw = str(req.form.get("use_raw_override", "false")).lower() in ("true", "1", "on")
                raw_json = req.form.get("raw_json", "").strip()
                if use_raw and raw_json:
                    try:
                        json.loads(raw_json)
                    except Exception as e:
                        return jsonify({"ret": "error", "message": f"올바른 JSON 형식이 아닙니다: {e}"})
                ModelSetting.set("use_raw_override", str(use_raw))
                ModelSetting.set("raw_json_override", raw_json)
                return jsonify({"ret": "success", "message": "JSON 에디터 내용이 저장되었습니다."})

        except Exception as e:
            logger.error(f"[ff_kradio] process_ajax 예외: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"ret": "error", "message": str(e)})

        return jsonify({"ret": "error", "message": "알 수 없는 요청입니다."})

    def process_api(self, sub, req):
        if sub == "channels.json":
            data = self.build_channels_json()
            return Response(
                json.dumps(data, ensure_ascii=False, indent=2),
                mimetype="application/json; charset=utf-8",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        elif sub == "xmltv.xml":
            xml_str = self.get_merged_xmltv(force=(req.args.get("force") == "true"))
            return Response(
                xml_str,
                mimetype="application/xml; charset=utf-8",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        elif sub == "audio":
            ch_id = req.args.get("id") or req.args.get("ch") or (req.view_args.get("ch_id") if req.view_args else None) or ""
            if not ch_id:
                return Response("채널 ID가 지정되지 않았습니다.", status=400, mimetype="text/plain; charset=utf-8")

            source_type = "tving"
            source_val = ch_id
            custom_channels = self.get_custom_channels()
            for ch in custom_channels:
                if ch.get("id") == ch_id:
                    source_type = ch.get("source_type", "tving")
                    source_val = ch.get("source_val", ch_id)
                    break

            apikey = self.get_setting("apikey") or ""

            if source_val.startswith("http://") or source_val.startswith("https://"):
                target_url = source_val
            elif source_val.startswith("C0") or source_type == "tving":
                target_url = f"http://127.0.0.1:8282/alive/api/url.m3u8?m=url&s=tving&i={source_val}&apikey={apikey}"
            else:
                target_url = source_val

            logger.info(f"[ff_kradio] ffmpeg 오디오 스트리밍 시작: ch_id={ch_id}, target={target_url}")

            cmd = [
                "ffmpeg",
                "-loglevel", "error",
                "-re",
                "-i", target_url,
                "-vn",
                "-c:a", "copy",
                "-f", "adts",
                "pipe:1"
            ]

            def generate():
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=32 * 1024
                )
                try:
                    while True:
                        chunk = proc.stdout.read(4096)
                        if not chunk:
                            break
                        yield chunk
                except (GeneratorExit, BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    try:
                        proc.kill()
                        proc.wait(timeout=1)
                    except Exception:
                        pass
                    logger.info(f"[ff_kradio] ffmpeg 오디오 스트리밍 종료: ch_id={ch_id}")

            return Response(
                stream_with_context(generate()),
                mimetype="audio/aac",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0"
                }
            )

        return Response("Not Found", status=404)


# ========================================================
# 공개 다이렉트 라우트 (API Key 완전 불필요)
# ========================================================
@P.blueprint.route("/channels.json")
def public_channels_json():
    try:
        mod = P.module_list[0]
        return mod.process_api("channels.json", request)
    except Exception as e:
        logger.error(f"[ff_kradio] public_channels_json 예외: {e}")
        return Response(str(e), status=500)

@P.blueprint.route("/xmltv.xml")
def public_xmltv():
    try:
        mod = P.module_list[0]
        return mod.process_api("xmltv.xml", request)
    except Exception as e:
        logger.error(f"[ff_kradio] public_xmltv 예외: {e}")
        return Response(str(e), status=500)

@P.blueprint.route("/audio")
@P.blueprint.route("/audio/<path:ch_id>")
def public_audio(ch_id=None):
    try:
        mod = P.module_list[0]
        return mod.process_api("audio", request)
    except Exception as e:
        logger.error(f"[ff_kradio] public_audio 예외: {e}")
        return Response(str(e), status=500)

@P.blueprint.route("/logo/<path:filename>")
def public_logo(filename):
    try:
        logo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo")
        return send_from_directory(logo_dir, filename)
    except Exception as e:
        logger.error(f"[ff_kradio] public_logo 예외: {e}")
        return Response("Logo Not Found", status=404)
