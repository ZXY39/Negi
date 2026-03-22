# agent_browser

鍐呯疆鐨?`agent_browser` 宸ュ叿鍙寘瑁呭畼鏂?`agent-browser` CLI锛屼笉 vendoring 涓婃父婧愮爜銆?

## 浣曟椂浣跨敤

- 闇€瑕佺湡瀹炴祻瑙堝櫒椤甸潰瀵艰埅銆佺偣鍑汇€佽緭鍏ャ€佹埅鍥炬垨浼氳瘽闅旂鏃躲€?
- 闇€瑕佷竴涓鍚?g3ku 璧勬簮娌荤悊銆佹潈闄愭帶鍒跺拰涓婁笅鏂囧姞杞借鑼冪殑娴忚鍣ㄥ伐鍏锋椂銆?
- 闇€瑕佽 agent 鍦?CLI 缂哄け鏃朵篃鑳藉厛璇诲埌瀹夎/鏇存柊甯姪鏃躲€?

## 瀹夎

- 涓婃父椤圭洰鍦板潃锛歚https://github.com/vercel-labs/agent-browser`
- 鎺ㄨ崘鎸変笂娓告枃妗ｅ畨瑁呭畼鏂?CLI锛屽苟纭繚 `agent-browser` 鍙粠 `PATH` 鐩存帴璋冪敤銆?
- 瀹夎鍚庯紝鍙敤 `exec` 妫€鏌ュ懡浠ゆ槸鍚﹀彲瑙侊紝渚嬪锛?
  - Windows: `where agent-browser`
  - macOS / Linux: `which agent-browser`
- 榛樿 `tools/agent_browser/resource.yaml -> settings.command_prefix` 浣跨敤 `agent-browser`锛屼緷璧栧綋鍓嶇幆澧?PATH 瑙ｆ瀽锛涘鏋滈渶瑕佽嚜瀹氫箟鍛戒护浣嶇疆锛屽彲鏀逛负缁濆璺緞鎴栧浐瀹氬墠缂€銆?

## 鏇存柊

- 浼樺厛鎸変笂娓镐粨搴撶殑瀹樻柟鏇存柊鏂瑰紡鏇存柊 CLI銆?
- 鏇存柊鍚庯紝閲嶆柊杩愯璺緞妫€鏌ュ懡浠わ紝纭 `agent-browser` 浠嶅彲浠?`PATH` 璋冪敤銆?
- 濡傛灉浣犱慨鏀硅繃 `command_prefix`锛岀‘璁ゆ洿鏂板悗鐨勫彲鎵ц鏂囦欢璺緞娌℃湁鍙樺寲銆?

## 浣跨敤

- 甯歌璋冪敤锛歚mode=run`锛屽苟閫氳繃 `args` 浼犻€掑師濮?CLI 鍙傛暟銆?
- 榛樿浼氫娇鐢?g3ku 涓撶敤 profile 鏍圭洰褰曪細`.g3ku/tool-data/agent_browser/profiles`
- 榛樿浼氭敞鍏ヤ細璇濆悕锛歚g3ku-agent-browser`
- 鑻ユ樉寮忎紶鍏ョ浉瀵?`profile`锛屼細鐩稿 workspace 瑙ｆ瀽骞惰嚜鍔ㄥ垱寤虹洰褰曘€?
- 濡傛灉 CLI 缂哄け锛屽彲鍏堣皟鐢細
  - `load_tool_context(tool_id="agent_browser")`
  - 鎴?`agent_browser(mode="install_help")`

## 鏁呴殰鎺掓煡

- 濡傛灉杩斿洖 `agent-browser CLI not found`锛氳鏄庡綋鍓嶇幆澧冮噷娌℃湁鎵惧埌 CLI锛屽彲鍏堟煡鐪嬪畨瑁呭府鍔╁苟浣跨敤 `exec` 妫€鏌?PATH銆?
- 濡傛灉鍑虹幇 `--profile ignored` 鎴?`daemon already running`锛氬伐鍏蜂細鍏堝皾璇曞叧闂綋鍓?session锛屽啀鑷姩閲嶈瘯涓€娆°€?
- 濡傛灉鍛戒护瓒呮椂锛氬伐鍏蜂細灏濊瘯鍏抽棴褰撳墠 session锛屽苟鍦ㄧ粨鏋滈噷杩斿洖 `session_cleanup` 缁嗚妭銆?

