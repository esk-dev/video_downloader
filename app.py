import os
import time
import zipfile
import random
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- Selenium Imports ---
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth

# --- КОНФИГУРАЦИЯ ---

PLAYLIST_FILE = "playlists.txt"
BASE_OUTPUT_FOLDER = "downloaded_playlists"
# Путь для перемещения готовых архивов
FINAL_DESTINATION_FOLDER = "/media/esk-dev/522EDD382EDD15B7" # ЗАМЕНИТЕ НА СВОЙ ПУТЬ

BASE_URL = "https://hypnotube.com"
REQUESTS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}
MAX_DOWNLOAD_THREADS = 4

# --- Опциональная обработка видео (отключена по умолчанию) ---
PERFORM_POST_PROCESSING = False
# ... (остальные настройки обработки без изменений)

# --- КОНЕЦ КОНФИГУРАЦИИ ---

def sanitize_filename(name):
    """Очищает имя файла от недопустимых символов."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def get_selenium_driver():
    """Создает и настраивает видимый экземпляр Selenium WebDriver."""
    chrome_options = ChromeOptions()
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument(f'user-agent={REQUESTS_HEADERS["User-Agent"]}')
    chrome_options.add_argument("window-size=1920,1080")
    chrome_options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    try:
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except Exception as e:
        print(f"❌ Не удалось инициализировать Chrome WebDriver: {e}")
        return None

    stealth(driver, languages=["en-US", "en"], vendor="Google Inc.", platform="Win32", webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True)
    driver.set_page_load_timeout(60)
    return driver

# ИЗМЕНЕНО: Функция теперь обходит все страницы плейлиста
def get_playlist_title_and_video_links(driver, playlist_url):
    """
    Анализирует все страницы плейлиста, используя существующий драйвер,
    и собирает все ссылки на видео.
    """
    print(f"🔍 Анализ плейлиста: {playlist_url}")
    try:
        driver.get(playlist_url)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/video/"]')))
        
        # Название плейлиста извлекаем один раз
        soup = BeautifulSoup(driver.page_source, "html.parser")
        playlist_title_tag = soup.select_one('h1.title, .playlist-title')
        if playlist_title_tag:
            playlist_title = playlist_title_tag.get_text(strip=True)
        else:
            try:
                path_parts = urlparse(playlist_url).path.strip('/').split('/')
                playlist_title = path_parts[-1] if path_parts[-1] else path_parts[-2]
            except:
                 playlist_title = "Untitled_Playlist_" + str(random.randint(1000, 9999))

        all_links = []
        found_urls = set()
        page_num = 1
        
        # --- НОВАЯ ЛОГИКА ПАГИНАЦИИ ---
        while True:
            print(f"📄 Анализ страницы {page_num}...")
            # Ждем, пока на странице появятся ссылки на видео (важно для переходов)
            WebDriverWait(driver, 20).until(EC.visibility_of_element_located((By.CSS_SELECTOR, 'a[href*="/video/"]')))
            
            page_soup = BeautifulSoup(driver.page_source, "html.parser")
            video_anchors = page_soup.select('a[href*="/video/"]')
            
            new_links_on_page = 0
            for a in video_anchors:
                href = a.get("href")
                if href and "/video/" in href and not any(x in href for x in ["/playlist/", "/user/", "/channel/"]):
                    full_url = href if href.startswith("http") else BASE_URL + href
                    if full_url not in found_urls:
                        all_links.append(full_url)
                        found_urls.add(full_url)
                        new_links_on_page += 1
            
            print(f"   -> Найдено {new_links_on_page} новых видео.")

            try:
                # Ищем кнопку "Next"
                next_page_button = driver.find_element(By.CSS_SELECTOR, "a[rel='next']")
                print("   -> Найдена кнопка 'Next', переход на следующую страницу...")
                # Используем JavaScript для клика, это надежнее
                driver.execute_script("arguments[0].scrollIntoView(true);", next_page_button)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", next_page_button)
                page_num += 1
                time.sleep(2) # Пауза, чтобы дать странице время на начало загрузки
            except NoSuchElementException:
                # Если кнопки нет, значит, это последняя страница
                print("✅ Достигнута последняя страница плейлиста.")
                break
        # --- КОНЕЦ ЛОГИКИ ПАГИНАЦИИ ---
        
        print(f"✅ Название плейлиста: '{playlist_title}'. Всего найдено уникальных видео: {len(all_links)} шт.")
        return playlist_title, all_links
    except Exception as e:
        print(f"❌ Ошибка при анализе плейлиста {playlist_url}: {e}")
        return None, []


def extract_mp4_link_and_title(driver, video_page_url):
    """Извлекает прямую ссылку на .mp4 и название видео с его страницы, выбирая наилучшее качество."""
    print(f"🔩 Обработка страницы: {video_page_url}")
    try:
        driver.get(video_page_url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "video")))
        time.sleep(1)
        
        soup = BeautifulSoup(driver.page_source, "html.parser")
        video_title = soup.select_one('h1').get_text(strip=True) if soup.select_one('h1') else "Untitled_Video"
        
        best_mp4_url = None
        # Ищем все теги <source> с атрибутом size, который указывает на качество
        source_tags = soup.select("video > source[src*='.mp4'][size]")
        
        if source_tags:
            # Создаем список словарей с качеством и ссылкой
            quality_options = [{"quality": int(tag.get('size', 0)), "url": tag.get('src')} for tag in source_tags]
            # Находим словарь с максимальным значением 'quality'
            best_option = max(quality_options, key=lambda x: x['quality'])
            best_mp4_url = best_option['url']
            print(f"   - Найдено видео '{video_title}'. Выбрано наилучшее качество: {best_option['quality']}p.")
        elif soup.select_one("video[src]"):
            best_mp4_url = soup.select_one("video[src]").get('src')
            print(f"   - Найдено видео '{video_title}'. Выбрано качество из основного тега <video>.")
        
        if best_mp4_url:
            if not best_mp4_url.startswith("http"):
                best_mp4_url = urlparse(video_page_url).scheme + ":" + best_mp4_url if best_mp4_url.startswith("//") else BASE_URL + best_mp4_url
            print(f"   -> 🔗 Ссылка для скачивания найдена.")
            return best_mp4_url, video_title
            
        print(f"   - ⚠️ Не удалось найти MP4 ссылку для видео '{video_title}'.")
        return None, video_title
    except Exception as e:
        print(f"❌ Неожиданная ошибка при обработке {video_page_url}: {e}")
        return None, "Untitled_Error_Video"

# --- Остальные функции (download_video, create_zip_archive и т.д.) без изменений ---
def download_video(mp4_url, title, output_folder, session):
    """Скачивает видеофайл."""
    sanitized_title = sanitize_filename(title)
    filepath = os.path.join(output_folder, f"{sanitized_title}.mp4")
    
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        print(f"   -> ✅ Файл '{sanitized_title}.mp4' уже существует, пропуск.")
        return filepath
        
    print(f"   -> 📥 Загрузка: {sanitized_title}.mp4")
    try:
        with session.get(mp4_url, stream=True, headers=REQUESTS_HEADERS, timeout=(30, 300)) as r:
            r.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192 * 4): # Увеличен chunk_size
                    f.write(chunk)
        return filepath
    except Exception as e:
        print(f"   -> ❌ Ошибка при загрузке '{sanitized_title}': {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None

def create_zip_archive(folder_to_zip, zip_name_base):
    """Архивирует папку и возвращает путь к созданному zip-файлу."""
    print(f"\n📦 Архивирование папки '{os.path.basename(folder_to_zip)}'...")
    try:
        final_zip_path = shutil.make_archive(zip_name_base, 'zip', folder_to_zip)
        print(f"✅ Архив успешно создан: {final_zip_path}")
        return final_zip_path
    except Exception as e:
        print(f"❌ Не удалось создать архив: {e}")
        return None

def get_requests_session_with_retries():
    """Создает сессию requests с настроенными повторными попытками."""
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('http://', HTTPAdapter(max_retries=retries))
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session

def transfer_cookies_from_selenium_to_requests(driver, session):
    """Переносит cookie из Selenium в сессию requests."""
    print("🍪 Перенос cookie из браузера в загрузчик...")
    for cookie in driver.get_cookies():
        session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
    print("✅ Cookie успешно перенесены.")

def main():
    if not os.path.exists(PLAYLIST_FILE):
        with open(PLAYLIST_FILE, "w") as f: f.write("# Вставьте сюда ссылки на плейлисты, по одной на строку\n")
        print(f"💡 Создан файл '{PLAYLIST_FILE}'. Пожалуйста, добавьте в него ссылки и перезапустите скрипт.")
        return
        
    if PERFORM_POST_PROCESSING and not shutil.which("ffmpeg"):
        print("❌ ОШИБКА: FFmpeg не найден, но включена опция постобработки.")
        return

    with open(PLAYLIST_FILE, "r") as f:
        playlist_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not playlist_urls:
        print(f"⚠️ Файл '{PLAYLIST_FILE}' пуст. Добавьте ссылки на плейлисты.")
        return
        
    os.makedirs(BASE_OUTPUT_FOLDER, exist_ok=True)
    
    driver = get_selenium_driver()
    if not driver:
        return

    try:
        driver.get(BASE_URL + "/")
        print("\n" + "="*80)
        print("🔴 ВАЖНО: Пожалуйста, вручную ВОЙДИТЕ в свой аккаунт в открывшемся окне браузера.")
        print("После успешного входа, вернитесь в эту консоль и нажмите [Enter] для продолжения.")
        print("="*80)
        input()

        try:
             WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='logout']")))
             print("✅ Отлично! Похоже, вы успешно вошли в систему.")
        except TimeoutException:
             print("⚠️ Не удалось автоматически подтвердить вход. Продолжаем на ваш страх и риск.")

        requests_session = get_requests_session_with_retries()
        transfer_cookies_from_selenium_to_requests(driver, requests_session)

        for url in playlist_urls:
            print(f"\n{'='*20} НАЧАЛО РАБОТЫ С ПЛЕЙЛИСТОМ: {url} {'='*20}")
            
            # Эта функция теперь возвращает ВСЕ ссылки со ВСЕХ страниц
            playlist_title, video_page_links = get_playlist_title_and_video_links(driver, url)
            
            if not video_page_links:
                print(f"❌ Не удалось получить ссылки на видео для плейлиста. Переход к следующему.")
                continue
                
            sanitized_playlist_title = sanitize_filename(playlist_title)
            playlist_folder = os.path.join(BASE_OUTPUT_FOLDER, sanitized_playlist_title)
            os.makedirs(playlist_folder, exist_ok=True)
            
            videos_to_download = []
            print("\n🔄 Сбор информации о видео...")
            for page_url in video_page_links:
                mp4_url, video_title = extract_mp4_link_and_title(driver, page_url)
                if mp4_url and video_title:
                    videos_to_download.append({"url": mp4_url, "title": video_title})
                time.sleep(random.uniform(0.5, 1.5))

            if not videos_to_download:
                print(f"❌ Не найдено ни одной ссылки для скачивания в плейлисте '{playlist_title}'.")
                continue

            print(f"\n📥 Начинается загрузка {len(videos_to_download)} видео в {MAX_DOWNLOAD_THREADS} потоков...")
            with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_THREADS) as executor:
                futures = [executor.submit(download_video, video['url'], video['title'], playlist_folder, requests_session) for video in videos_to_download]
                for future in as_completed(futures):
                    future.result()

            # --- Логика архивации, перемещения и удаления (без изменений) ---
            zip_base_name = os.path.join(BASE_OUTPUT_FOLDER, sanitized_playlist_title)
            created_zip_file = create_zip_archive(playlist_folder, zip_base_name)

            if created_zip_file:
                if os.path.exists(FINAL_DESTINATION_FOLDER):
                    try:
                        print(f"🚚 Перемещение '{os.path.basename(created_zip_file)}' в '{FINAL_DESTINATION_FOLDER}'...")
                        shutil.move(created_zip_file, FINAL_DESTINATION_FOLDER)
                        print("   -> ✅ Перемещение завершено.")
                        
                        try:
                            print(f"🗑️ Удаление исходной папки: {playlist_folder}")
                            shutil.rmtree(playlist_folder)
                            print("   -> ✅ Папка успешно удалена.")
                        except Exception as e:
                            print(f"   -> ❌ Ошибка при удалении папки '{playlist_folder}': {e}")
                            
                    except Exception as e:
                        print(f"❌ Ошибка при перемещении архива '{created_zip_file}': {e}")
                        print("   -> ⚠️ Исходная папка с видео НЕ будет удалена из-за ошибки перемещения.")
                else:
                    print(f"❌ ОШИБКА: Конечная папка '{FINAL_DESTINATION_FOLDER}' не существует!")
                    print(f"   -> ⚠️ Архив '{os.path.basename(created_zip_file)}' сохранен в папке со скриптом.")
            
            print(f"\n{'='*20} РАБОТА С ПЛЕЙЛИСТОМ '{playlist_title}' ЗАВЕРШЕНА {'='*20}")

    finally:
        if 'driver' in locals() and driver:
            print("\n🏁 Закрытие браузера...")
            driver.quit()

    print("\n\n🎉🎉🎉 ВСЕ ЗАДАЧИ ВЫПОЛНЕНЫ! 🎉🎉🎉")

if __name__ == "__main__":
    main()