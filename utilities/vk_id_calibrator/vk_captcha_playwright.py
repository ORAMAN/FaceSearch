#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Полноценный Chromium/Playwright для ручной VK SmartCaptcha.

Использует постоянный профиль Chromium, выбранный пользователем. Пароли
не экспортируются; токен проверки, User-Agent и необходимые cookies передаются
основному приложению через временный локальный IPC-файл для повтора запроса VK.
"""


from __future__ import annotations

HELPER_BUILD = "2026-10-31-browser-bound-init-2"

import json
import os
import sys
import tempfile
import platform
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qs


def write_result(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def find_token(value: Any, depth: int = 0) -> str | None:
    if depth > 10:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('success_token', 'successToken') and isinstance(item, str) and len(item) >= 8:
                return item
        for item in value.values():
            found = find_token(item, depth + 1)
            if found: return found
    elif isinstance(value, list):
        for item in value:
            found = find_token(item, depth + 1)
            if found: return found
    return None


def safe_error_message(exc: BaseException, original_url: str) -> str:
    """Не допускаем попадания query-параметров redirect_uri в диагностический текст."""
    message = f"{type(exc).__name__}: {exc}"
    parsed = urlparse(original_url)
    safe_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    if original_url:
        message = message.replace(original_url, safe_url)
    # Некоторые ошибки драйвера могут повторить URL с добавленными параметрами.
    def redact(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            item = urlparse(raw)
            if item.hostname in {"id.vk.ru", "id.vk.com"}:
                return urlunparse((item.scheme, item.netloc, item.path, "", "", ""))
        except Exception:
            pass
        return raw
    return re.sub(r"https?://[^\s\"'<>]+", redact, message)[:1800]


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    result_path = Path(sys.argv[1])
    url = ""
    retry_url = ""
    retry_params: dict[str, Any] = {}
    profile = Path.cwd()
    diagnostics: list[str] = [f"Python: {sys.version.split()[0]} ({sys.executable})",
                              f"OS: {platform.platform()}"]
    try:
        payload = json.loads(sys.stdin.read() or '{}')
        url = str(payload.get('url') or '')
        profile = Path(str(payload.get('profile_dir') or '')).expanduser().resolve()
        browser_executable = str(payload.get('browser_executable') or '').strip()
        retry_url = str(payload.get('retry_url') or '').strip()
        raw_retry_params = payload.get('retry_params')
        if not isinstance(raw_retry_params, dict):
            raise ValueError('Не переданы параметры повторного API-запроса.')
        retry_params = {str(k): str(v) for k, v in raw_retry_params.items()}
        retry_parsed = urlparse(retry_url)
        if (retry_parsed.scheme != 'https'
                or retry_parsed.hostname not in {'api.vk.com', 'api.vk.ru'}
                or not retry_parsed.path.endswith('/method/restore.init')):
            raise ValueError('Недопустимый адрес повторного API-запроса.')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in {'id.vk.ru', 'id.vk.com'} or parsed.path.rstrip('/') != '/not_robot_captcha':
            raise ValueError('VK передал недопустимый адрес страницы проверки.')
        if not str(profile):
            raise ValueError('Не задан каталог профиля браузера.')
        profile.mkdir(parents=True, exist_ok=True)
        diagnostics.append(f"Профиль Chromium: {profile}")
        diagnostics.append(f"Папка профиля доступна для записи: {os.access(profile, os.W_OK)}")
        try:
            from playwright.sync_api import sync_playwright
            import importlib.metadata
            try:
                playwright_version = importlib.metadata.version('playwright')
            except importlib.metadata.PackageNotFoundError:
                playwright_version = 'версия неизвестна'
            diagnostics.append(f"Playwright: {playwright_version}")
        except ImportError as exc:
            raise RuntimeError('Playwright не установлен в выбранном Python. Запустите start_vk_id_calibrator.bat.') from exc

        token_reported = False
        success_token_value = ''
        captcha_capture_error = ''
        end_session_reported = False
        end_session_http_status: int | None = None
        # Старый redirect_uri получен запросом из Python без браузерных cookies.
        # Он используется только для валидации, но не для решения CAPTCHA:
        # ниже restore.getSettings и первоначальный restore.init будут выполнены
        # из того же Chrome, который затем откроет новый redirect_uri.
        challenge_session_token = ''
        browser_session_update: dict[str, Any] = {}
        browser_initial_error: dict[str, Any] = {}

        # Только булевы признаки и имена параметров попадут в IPC/журнал.
        # Сами cookie, session_token и success_token остаются в памяти процесса.
        network_diag: dict[str, Any] = {
            'challenge_session_present': True,
            'check_seen': False,
            'check_session_matches_challenge': None,
            'check_vk_status': None,
            'check_http_status': None,
            'check_remixstlid_present': False,
            'check_remixstid_present': False,
            'check_user_agent_present': False,
            'browser_initial_init_seen': False,
            'browser_initial_remixstlid_present': False,
            'browser_initial_origin': None,
            'browser_initial_referer': None,
            'browser_initial_vs_check_remixstlid_matches': None,
            'browser_initial_vs_check_user_agent_matches': None,
            'end_session_seen': False,
            'end_session_session_matches_challenge': None,
            'retry_seen': False,
            'retry_remixstlid_present': False,
            'retry_remixstid_present': False,
            'retry_domain_sid_present': False,
            'retry_session_cookie_matches_check': None,
            'retry_remixstid_matches_check': None,
            'retry_domain_sid_matches_check': None,
            'retry_user_agent_matches_check': None,
            'retry_origin': None,
            'retry_referer': None,
            'retry_field_matches': {},
            'server_echo_matches': {},
        }
        check_cookie_values: dict[str, str] = {}
        browser_initial_cookie_values: dict[str, str] = {}
        check_request_headers: dict[str, str] = {}
        browser_initial_request_headers: dict[str, str] = {}
        check_user_agent_value = ''
        browser_initial_user_agent = ''
        browser_initial_observation_armed = False
        retry_observation_armed = False

        def _request_info(request):
            try:
                headers = request.all_headers()
            except Exception:
                try:
                    headers = request.headers
                except Exception:
                    headers = {}
            try:
                raw_body = request.post_data or ''
            except Exception:
                raw_body = ''
            try:
                parsed_form = parse_qs(raw_body, keep_blank_values=True)
                form = {k: values[-1] for k, values in parsed_form.items()}
            except Exception:
                form = {}
            cookies: dict[str, str] = {}
            for part in str(headers.get('cookie', '')).split(';'):
                if '=' in part:
                    name, value = part.strip().split('=', 1)
                    cookies[name] = value
            return headers, form, cookies

        def on_request(request):
            nonlocal check_user_agent_value, browser_initial_user_agent
            try:
                parsed_request_url = urlparse(request.url)
                host = (parsed_request_url.hostname or '').lower()
                path = parsed_request_url.path.lower()
                headers, form, cookies = _request_info(request)
                if browser_initial_observation_armed and host in {'api.vk.com', 'api.vk.ru'} and path.endswith('/method/restore.init'):
                    browser_initial_cookie_values.update(cookies)
                    browser_initial_request_headers.update({k: str(v) for k, v in headers.items()})
                    browser_initial_user_agent = str(headers.get('user-agent') or '')
                    network_diag['browser_initial_init_seen'] = True
                    network_diag['browser_initial_remixstlid_present'] = bool(cookies.get('remixstlid'))
                    network_diag['browser_initial_origin'] = str(headers.get('origin') or '')
                    network_diag['browser_initial_referer'] = str(headers.get('referer') or '')
                elif host == 'api.vk.com' and path.endswith('/captchanotrobot.check'):
                    network_diag['check_seen'] = True
                    network_diag['check_session_matches_challenge'] = bool(
                        form.get('session_token') and form.get('session_token') == challenge_session_token
                    )
                    network_diag['check_remixstlid_present'] = bool(cookies.get('remixstlid'))
                    network_diag['check_remixstid_present'] = bool(cookies.get('remixstid'))
                    network_diag['check_domain_sid_present'] = bool(cookies.get('domain_sid'))
                    check_cookie_values.update(cookies)
                    check_request_headers.update({k: str(v) for k, v in headers.items()})
                    check_user_agent_value = str(headers.get('user-agent') or '')
                    network_diag['check_user_agent_present'] = bool(check_user_agent_value)
                    init_stlid = browser_initial_cookie_values.get('remixstlid', '')
                    check_stlid = cookies.get('remixstlid', '')
                    network_diag['browser_initial_vs_check_remixstlid_matches'] = bool(init_stlid and check_stlid and init_stlid == check_stlid)
                    network_diag['browser_initial_vs_check_user_agent_matches'] = bool(browser_initial_user_agent and check_user_agent_value and browser_initial_user_agent == check_user_agent_value)
                elif host == 'api.vk.com' and path.endswith('/captchanotrobot.endsession'):
                    network_diag['end_session_seen'] = True
                    network_diag['end_session_session_matches_challenge'] = bool(
                        form.get('session_token') and form.get('session_token') == challenge_session_token
                    )
                elif retry_observation_armed and host in {'api.vk.com', 'api.vk.ru'} and path.endswith('/method/restore.init'):
                    network_diag['retry_seen'] = True
                    network_diag['retry_remixstlid_present'] = bool(cookies.get('remixstlid'))
                    network_diag['retry_remixstid_present'] = bool(cookies.get('remixstid'))
                    network_diag['retry_domain_sid_present'] = bool(cookies.get('domain_sid'))
                    retry_cookie = cookies.get('remixstlid', '')
                    checked_cookie = check_cookie_values.get('remixstlid', '')
                    retry_remixstid = cookies.get('remixstid', '')
                    checked_remixstid = check_cookie_values.get('remixstid', '')
                    retry_domain_sid = cookies.get('domain_sid', '')
                    checked_domain_sid = check_cookie_values.get('domain_sid', '')
                    network_diag['retry_session_cookie_matches_check'] = bool(
                        retry_cookie and checked_cookie and retry_cookie == checked_cookie
                    )
                    network_diag['retry_remixstid_matches_check'] = bool(
                        retry_remixstid and checked_remixstid and retry_remixstid == checked_remixstid
                    )
                    network_diag['retry_domain_sid_matches_check'] = bool(
                        retry_domain_sid and checked_domain_sid and retry_domain_sid == checked_domain_sid
                    )
                    retry_ua = str(headers.get('user-agent') or '')
                    network_diag['retry_user_agent_matches_check'] = bool(
                        retry_ua and check_user_agent_value and retry_ua == check_user_agent_value
                    )
                    network_diag['retry_origin'] = str(headers.get('origin') or '')
                    network_diag['retry_referer'] = str(headers.get('referer') or '')
                    expected = dict(retry_params)
                    expected['success_token'] = success_token_value
                    keys_to_compare = (
                        'success_token', 'captcha_sid', 'captcha_ts', 'captcha_attempt',
                        'captcha_key', 'is_sound_captcha', 'uid', 'restore_session_id',
                        'unauth_id', 'v'
                    )
                    network_diag['retry_field_matches'] = {
                        key: (key in form and str(form.get(key, '')) == str(expected.get(key, '')))
                        for key in keys_to_compare if key in expected
                    }
            except Exception:
                # Диагностика не должна прерывать обработку CAPTCHA.
                pass

        with sync_playwright() as p:
            if browser_executable:
                executable = Path(browser_executable).expanduser().resolve()
                diagnostics.append(f"Выбранный браузер: {executable}")
                diagnostics.append(f"Файл браузера найден: {executable.is_file()}")
                if not executable.is_file():
                    raise RuntimeError(f"Указанный файл браузера не найден: {executable}")
            else:
                executable = Path(p.chromium.executable_path)
                diagnostics.append(f"Chromium Playwright: {executable}")
                diagnostics.append(f"Chromium Playwright найден: {executable.is_file()}")
                if not executable.is_file():
                    raise RuntimeError(
                        "Chromium Playwright не установлен. Укажите путь к Chrome в настройках "
                        "или запустите start_vk_id_calibrator.bat для установки Chromium."
                    )
            # Persistent context использует отдельный профиль, выбранный в настройках.
            # Не подключаем одновременно обычный профиль работающего Chrome/Edge.
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile), executable_path=str(executable), headless=False,
                    viewport={'width': 1180, 'height': 900},
                    timeout=60000,
                    # Обычный пользовательский режим: без DevTools и отладочных пауз.
                    args=['--start-maximized'],
                )
            except Exception as exc:
                msg = str(exc).lower()
                if 'user data directory is already in use' in msg or 'processsingleton' in msg or 'profile' in msg and 'lock' in msg:
                    raise RuntimeError(
                        f"Каталог профиля занят другим процессом Chromium: {profile}. "
                        "Закройте все окна Chromium, работающие с этой папкой, либо выберите "
                        "отдельный каталог профиля в настройках приложения."
                    ) from exc
                raise RuntimeError(
                    f"Playwright не смог запустить Chromium ({type(exc).__name__}): {exc}. "
                    f"Проверьте выбранный браузер и права на каталог профиля: {profile}"
                ) from exc
            page = context.pages[0] if context.pages else context.new_page()

            def capture_browser_context() -> dict[str, Any]:
                '''Возвращает только нужные для последующих API-запросов cookie и заголовки.

                Данные передаются через локальный временный IPC-файл основному процессу,
                который удаляет его сразу после чтения. В постоянные настройки/логи
                значения cookies не попадают.
                '''
                allowed_cookie_names = ('remixstid', 'remixstlid', 'domain_sid')
                jar_values: dict[str, str] = {}
                try:
                    for item in context.cookies(['https://api.vk.com']):
                        name = str(item.get('name') or '')
                        domain = str(item.get('domain') or '').lstrip('.').lower()
                        value = str(item.get('value') or '')
                        if name in allowed_cookie_names and value and (domain == 'vk.com' or domain.endswith('.vk.com')):
                            jar_values[name] = value
                except Exception:
                    pass

                # Browser request headers captured from the real challenge flow are
                # preferable to reconstructing Client Hints from navigator values.
                observed = dict(check_cookie_values or browser_initial_cookie_values)
                cookies = {name: (jar_values.get(name) or observed.get(name) or '')
                           for name in allowed_cookie_names}
                cookie_header = '; '.join(f'{name}={cookies[name]}' for name in allowed_cookie_names if cookies[name])

                user_agent = check_user_agent_value or browser_initial_user_agent
                if not user_agent:
                    try:
                        user_agent = str(page.evaluate('() => navigator.userAgent') or '')
                    except Exception:
                        user_agent = ''

                # Для обычных restore.init берём Origin/Referer именно исходного
                # браузерного запроса; check идёт с id.vk.com и может иметь другой origin.
                header_source = browser_initial_request_headers or check_request_headers
                safe_headers = {
                    key: str(header_source.get(key) or '')
                    for key in (
                        'origin', 'referer', 'accept-language', 'sec-ch-ua',
                        'sec-ch-ua-mobile', 'sec-ch-ua-platform',
                        'sec-fetch-site', 'sec-fetch-mode', 'sec-fetch-dest', 'priority'
                    )
                    if header_source.get(key)
                }
                return {
                    'cookie_header': cookie_header,
                    'cookie_names': [name for name in allowed_cookie_names if cookies[name]],
                    'user_agent': user_agent,
                    'headers': safe_headers,
                }

            def on_response(response):
                nonlocal token_reported, success_token_value, end_session_reported, end_session_http_status, captcha_capture_error, check_user_agent_value
                try:
                    parsed_response_url = urlparse(response.url)
                    host = (parsed_response_url.hostname or '').lower()
                    path = parsed_response_url.path.lower()

                    # Важно дождаться endSession: страница VK завершает
                    # CAPTCHA-сессию отдельным запросом после check.
                    if host == 'api.vk.com' and path.endswith('/captchanotrobot.endsession'):
                        end_session_http_status = response.status
                        _, end_form, _ = _request_info(response.request)
                        end_same_challenge = bool(
                            end_form.get('session_token') and end_form.get('session_token') == challenge_session_token
                        )
                        network_diag['end_session_seen'] = True
                        network_diag['end_session_session_matches_challenge'] = end_same_challenge
                        try:
                            end_data = response.json()
                            end_resp = end_data.get('response') if isinstance(end_data, dict) else None
                            end_session_reported = bool(
                                response.ok and end_same_challenge and isinstance(end_resp, dict)
                                and end_resp.get('status') == 'OK'
                            )
                        except Exception:
                            end_session_reported = bool(response.ok and end_same_challenge)
                        return

                    # VK ID отправляет результат SmartCaptcha не с id.vk.com,
                    # а с api.vk.com/method/captchaNotRobot.check.
                    if token_reported or host != 'api.vk.com' or not path.endswith('/captchanotrobot.check'):
                        return
                    req_headers, req_form, req_cookies = _request_info(response.request)
                    request_session = req_form.get('session_token', '')
                    same_challenge = bool(request_session and request_session == challenge_session_token)
                    network_diag['check_seen'] = True
                    network_diag['check_session_matches_challenge'] = same_challenge
                    network_diag['check_http_status'] = response.status
                    network_diag['check_remixstlid_present'] = bool(req_cookies.get('remixstlid'))
                    network_diag['check_remixstid_present'] = bool(req_cookies.get('remixstid'))
                    check_cookie_values.update(req_cookies)
                    check_user_agent_value = str(req_headers.get('user-agent') or '')
                    network_diag['check_user_agent_present'] = bool(check_user_agent_value)
                    data = response.json()
                    response_payload = data.get('response') if isinstance(data, dict) else None
                    vk_status = response_payload.get('status') if isinstance(response_payload, dict) else None
                    network_diag['check_vk_status'] = vk_status
                    token = find_token(data)
                    if token and response.ok and str(vk_status).upper() == 'OK' and same_challenge:
                        success_token_value = token
                        token_reported = True
                        # Сам success_token остаётся только в памяти процесса Chromium.
                        # В IPC передаём лишь флаг получения токена.
                        write_result(result_path, {
                            'token_obtained': True,
                            'response_url': f"{parsed_response_url.scheme}://{parsed_response_url.netloc}{parsed_response_url.path}",
                            'response_status': response.status,
                            'vk_status': vk_status,
                        })
                    elif token:
                        captcha_capture_error = (
                            'VK вернул success_token, но POST captchaNotRobot.check не относится к '
                            'session_token из текущего redirect_uri. Повтор restore.init отменён, '
                            'чтобы не отправлять токен от другой CAPTCHA-сессии.'
                        )
                        diagnostics.append(
                            'Ответ check содержал success_token, но не прошёл привязку к текущему challenge '
                            f'(HTTP={response.status}, VK status={vk_status}, session_match={same_challenge}).'
                        )
                except Exception as exc:
                    diagnostics.append(f"Не удалось обработать сетевой ответ CAPTCHA: {type(exc).__name__}")

            page.on('request', on_request)
            page.on('response', on_response)

            # Критически важный этап: исходный restore.init в прежней схеме
            # отправлялся из urllib без remixstlid, а CAPTCHA и повтор — из Chrome.
            # Получаем свежие restore settings и создаём новый challenge внутри
            # того же браузерного профиля, где пользователь завершит SmartCaptcha.
            try:
                page.goto('https://vk.com/', wait_until='domcontentloaded', timeout=120000)
                page.goto('https://id.vk.com/', wait_until='domcontentloaded', timeout=120000)
            except Exception as exc:
                diagnostics.append(f"Подготовка браузерной сессии: {safe_error_message(exc, url)}")
                if page.is_closed() or page.url in ("", "about:blank"):
                    raise RuntimeError("Не удалось открыть VK перед созданием браузерного challenge.") from exc

            base_params = {
                key: value for key, value in retry_params.items()
                if key not in {'captcha_key', 'captcha_sid', 'is_sound_captcha', 'captcha_ts', 'captcha_attempt', 'success_token'}
            }
            bootstrap_params = {
                'uid': base_params.get('uid', ''),
                'access_token': base_params.get('access_token', ''),
                'v': base_params.get('v', '5.223'),
            }
            browser_initial_observation_armed = True
            bootstrap = page.evaluate("""async ({settingsUrl, initUrl, baseParams}) => {
                const postForm = async (url, params) => {
                    const body = new URLSearchParams();
                    for (const [key, value] of Object.entries(params)) {
                        if (value !== undefined && value !== null && String(value) !== '') body.append(key, String(value));
                    }
                    const response = await fetch(url, {
                        method: 'POST', mode: 'cors', credentials: 'include', cache: 'no-store',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: body.toString()
                    });
                    let data;
                    try { data = await response.json(); }
                    catch (_) { data = null; }
                    return {status: response.status, ok: response.ok, data};
                };
                const settings = await postForm(settingsUrl, {
                    type: 'restore', vkui: '1', platform: 'vkcom', access_token: '', v: '5.83'
                });
                const settingsData = settings.data && settings.data.response;
                if (!settings.ok || !settingsData || !settingsData.restore_session_id) {
                    return {stage: 'getSettings', http_status: settings.status,
                        error_code: settings.data?.error?.error_code ?? null,
                        error_msg: settings.data?.error?.error_msg ?? 'restore.getSettings не вернул restore_session_id'};
                }
                const params = {...baseParams, restore_session_id: settingsData.restore_session_id};
                if (settingsData.unauth_id !== undefined && settingsData.unauth_id !== null && String(settingsData.unauth_id) !== '') {
                    params.unauth_id = settingsData.unauth_id;
                } else {
                    delete params.unauth_id;
                }
                const init = await postForm(initUrl, params);
                const data = init.data || {};
                const err = data.error || null;
                if (err) {
                    return {
                        stage: 'restore.init', http_status: init.status,
                        session: {restore_session_id: settingsData.restore_session_id, unauth_id: settingsData.unauth_id ?? ''},
                        error: {
                            error_code: err.error_code ?? null,
                            error_msg: err.error_msg ?? 'Ошибка restore.init',
                            redirect_uri: err.redirect_uri ?? '',
                            captcha_sid: err.captcha_sid ?? '',
                            captcha_ts: err.captcha_ts ?? 0,
                            captcha_attempt: err.captcha_attempt ?? 1
                        }
                    };
                }
                return {stage: 'restore.init', http_status: init.status,
                    session: {restore_session_id: settingsData.restore_session_id, unauth_id: settingsData.unauth_id ?? ''},
                    response: data};
            }""", {
                'settingsUrl': retry_url.rsplit('/', 1)[0] + '/restore.getSettings',
                'initUrl': retry_url,
                'baseParams': bootstrap_params,
            })
            browser_initial_observation_armed = False
            if not isinstance(bootstrap, dict):
                raise RuntimeError('Браузер не вернул результат первичной инициализации restore.init.')
            if bootstrap.get('stage') == 'getSettings':
                raise RuntimeError(
                    f"Браузерный restore.getSettings не выполнен (HTTP {bootstrap.get('http_status', '?')}, "
                    f"код {bootstrap.get('error_code')}). {bootstrap.get('error_msg', '')}"
                )
            browser_session_update = bootstrap.get('session') if isinstance(bootstrap.get('session'), dict) else {}
            browser_initial_error = bootstrap.get('error') if isinstance(bootstrap.get('error'), dict) else {}
            if not browser_initial_error:
                # В браузерном контексте CAPTCHA не потребовалась: возвращаем
                # результат обновлённого restore.init без открытия challenge.
                browser_response = bootstrap.get('response')
                if not isinstance(browser_response, dict):
                    raise RuntimeError('Браузерный restore.init вернул ответ неожиданного формата.')
                write_result(result_path, {
                    'token_obtained': False,
                    'captcha_performed': False,
                    'retry_response': browser_response,
                    'retry_http_status': bootstrap.get('http_status'),
                    'session_update': browser_session_update,
                    'browser_context': capture_browser_context(),
                    'retry_diagnostics': network_diag,
                })
                diagnostics.append('Браузерный restore.init завершился без CAPTCHA; возвращаю результат.')
                try:
                    context.close()
                except Exception:
                    pass
                return 0

            if int(browser_initial_error.get('error_code') or 0) != 14:
                safe_error = {
                    'error_code': browser_initial_error.get('error_code'),
                    'error_msg': browser_initial_error.get('error_msg'),
                }
                write_result(result_path, {
                    'token_obtained': False,
                    'captcha_performed': False,
                    'retry_response': {'error': safe_error},
                    'retry_http_status': bootstrap.get('http_status'),
                    'session_update': browser_session_update,
                    'retry_diagnostics': network_diag,
                })
                diagnostics.append('Браузерный restore.init вернул ошибку, не связанную с CAPTCHA.')
                try:
                    context.close()
                except Exception:
                    pass
                return 0

            url = str(browser_initial_error.get('redirect_uri') or '')
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname not in {'id.vk.ru', 'id.vk.com'} or parsed.path.rstrip('/') != '/not_robot_captcha':
                raise RuntimeError('Браузерный restore.init получил CAPTCHA, но redirect_uri отсутствует или имеет неожиданный адрес.')
            challenge_session_token = (parse_qs(parsed.query).get('session_token') or [''])[0]
            if not challenge_session_token:
                raise RuntimeError('В новом redirect_uri отсутствует session_token; CAPTCHA отменена для защиты от повтора устаревшего challenge.')
            retry_params = dict(base_params)
            retry_params['restore_session_id'] = str(browser_session_update.get('restore_session_id') or '')
            if browser_session_update.get('unauth_id') not in (None, ''):
                retry_params['unauth_id'] = str(browser_session_update.get('unauth_id'))
            else:
                retry_params.pop('unauth_id', None)
            retry_params.update({
                'captcha_key': '',
                'captcha_sid': str(browser_initial_error.get('captcha_sid') or ''),
                'is_sound_captcha': '0',
                'captcha_ts': str(browser_initial_error.get('captcha_ts') or 0),
                'captcha_attempt': str(browser_initial_error.get('captcha_attempt') or 1),
            })
            network_diag['challenge_session_present'] = True
            diagnostics.append('Свежий restore.init повторно выполнен внутри Chrome после получения cookies VK.')
            diagnostics.append('CAPTCHA теперь создаётся в том же браузерном контексте, в котором будет пройдена проверка и выполнен повтор.')
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=120000)
            except Exception as exc:
                diagnostics.append(f"Навигация CAPTCHA: {safe_error_message(exc, url)}")
                if page.is_closed() or page.url in ("", "about:blank"):
                    raise RuntimeError("Chromium запущен, но новая страница CAPTCHA не открылась.") from exc
            diagnostics.append("Chromium успешно запущен; страница CAPTCHA открыта.")
            # Сначала ждём success_token, затем — завершение captchaNotRobot.endSession.
            # Не закрываем страницу немедленно после check: фронтенд VK отправляет
            # endSession отдельным POST, который должен успеть завершиться.
            while not token_reported and not captcha_capture_error:
                try:
                    if page.is_closed():
                        break
                    page.wait_for_timeout(150)
                except Exception:
                    if token_reported:
                        break
                    raise

            if captcha_capture_error:
                diagnostics.append(captcha_capture_error)
                try:
                    context.close()
                except Exception:
                    pass
                write_result(result_path, {
                    'error': captcha_capture_error,
                    'diagnostics': "\n".join(diagnostics[-12:]),
                    'retry_diagnostics': network_diag,
                })
                return 0

            if token_reported:
                diagnostics.append("success_token получен и привязан к текущему session_token. Ожидаю завершения captchaNotRobot.endSession.")
                end_deadline = time.monotonic() + 6.0
                while not end_session_reported and time.monotonic() < end_deadline:
                    try:
                        if page.is_closed():
                            break
                        page.wait_for_timeout(100)
                    except Exception:
                        break

                if end_session_reported:
                    diagnostics.append("captchaNotRobot.endSession завершён успешно.")
                else:
                    diagnostics.append("captchaNotRobot.endSession не подтверждён за 6 секунд; продолжаю повтор по тайм-ауту.")

                # Ключевое отличие: повтор restore.init выполняется из самой страницы
                # VK ID через браузерный fetch. Так Chrome сам формирует TLS,
                # Client-Hints, Origin/Referer, Sec-Fetch и прикладывает cookies.
                # В Python больше не воспроизводится браузерный запрос через urllib.
                try:
                    replay_params = dict(retry_params)
                    replay_params['success_token'] = success_token_value
                    retry_observation_armed = True
                    retry_result = page.evaluate("""async ({url, params}) => {
                        const body = new URLSearchParams();
                        for (const [key, value] of Object.entries(params)) {
                            body.append(key, value == null ? '' : String(value));
                        }
                        const sentValues = Object.fromEntries(body.entries());
                        const response = await fetch(url, {
                            method: 'POST',
                            mode: 'cors',
                            credentials: 'include',
                            cache: 'no-store',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                            body: body.toString()
                        });
                        const raw = await response.text();
                        let data;
                        try { data = JSON.parse(raw); }
                        catch (_) { data = {non_json_response: raw.slice(0, 1000)}; }

                        // Не переносим токены/redirect_uri/значения request_params
                        // во временный IPC-файл. Для диагностики достаточно имён.
                        if (data && data.error && typeof data.error === 'object') {
                            const err = data.error;
                            let pairs = [];
                            if (Array.isArray(err.request_params)) {
                                pairs = err.request_params.filter(x => x && x.key).map(x => ({key: String(x.key), value: x.value}));
                            } else if (err.request_params && typeof err.request_params === 'object') {
                                pairs = Object.entries(err.request_params).map(([key, value]) => ({key, value}));
                            }
                            const safeParams = pairs.map(({key, value}) => ({
                                key,
                                matches_sent_value: Object.prototype.hasOwnProperty.call(sentValues, key)
                                  ? String(sentValues[key]) === String(value ?? '') : null
                            }));
                            data = {error: {
                                error_code: err.error_code,
                                error_msg: err.error_msg,
                                request_params: safeParams
                            }};
                        }
                        return {http_status: response.status, http_ok: response.ok, data};
                    }""", {'url': retry_url, 'params': replay_params})
                    if not isinstance(retry_result, dict):
                        raise RuntimeError('Браузер вернул ответ неожиданного формата при повторе restore.init.')
                    retry_data = retry_result.get('data')
                    if isinstance(retry_data, dict) and isinstance(retry_data.get('error'), dict):
                        echoed = retry_data['error'].get('request_params')
                        if isinstance(echoed, list):
                            network_diag['server_echo_matches'] = {
                                str(item.get('key')): item.get('matches_sent_value')
                                for item in echoed if isinstance(item, dict) and item.get('key')
                                and item.get('matches_sent_value') is not None
                            }
                    final_result = {
                        'token_obtained': True,
                        'captcha_performed': True,
                        'session_update': browser_session_update,
                        'browser_context': capture_browser_context(),
                        'response_status': 200,
                        'end_session_ok': end_session_reported,
                        'end_session_http_status': end_session_http_status,
                        'retry_via_browser': True,
                        'retry_http_status': retry_result.get('http_status'),
                        'retry_http_ok': retry_result.get('http_ok'),
                        'retry_response': retry_result.get('data'),
                        'retry_diagnostics': network_diag,
                    }
                    write_result(result_path, final_result)
                    diagnostics.append(f"Повтор restore.init выполнен браузером Chrome (HTTP {retry_result.get('http_status', '?')}).")
                except Exception as exc:
                    write_result(result_path, {
                        'token_obtained': True,
                        'end_session_ok': end_session_reported,
                        'end_session_http_status': end_session_http_status,
                        'retry_via_browser': True,
                        'retry_error': safe_error_message(exc, url),
                    })
                    diagnostics.append(f"Не удалось выполнить restore.init из браузера: {type(exc).__name__}: {str(exc)[:500]}")

                diagnostics.append("Повтор завершён. Автоматически закрываю Chromium.")
                try:
                    context.close()
                except Exception:
                    pass
            else:
                try:
                    context.close()
                except Exception:
                    pass
                raise RuntimeError("Окно Chromium закрыто до получения success_token. Пройдите CAPTCHA и не закрывайте браузер до подтверждения.")
        return 0
    except Exception as exc:
        # Защита от перезаписи успешно сохранённого токена диагностикой
        # неожиданного закрытия браузера.
        try:
            if result_path.is_file():
                existing = json.loads(result_path.read_text(encoding='utf-8'))
                if isinstance(existing, dict) and existing.get('retry_response') is not None:
                    return 0
        except Exception:
            pass
        diagnostic_text = "\n".join(diagnostics[-12:])
        try:
            write_result(result_path, {
                'error': safe_error_message(exc, url),
                'diagnostics': diagnostic_text,
            })
        except Exception:
            pass
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
