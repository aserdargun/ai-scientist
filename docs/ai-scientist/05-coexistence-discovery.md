# AOS / Lab birlikte çalışma keşfi

Tarih: 2026-09-24. Bu not uygulama önerilerini gerçek mevcut kaynak koduna bağlar; henüz birlikte GPU kabulü değildir.

## AOS GPU ömrü

- `../aos/src/aos/reusable_decider.py:157`: `decide()` istek sonunda worker'ı kapatmaz. `request()` içindeki asyncio.Lock yalnız aynı Python nesnesinin isteklerini korur, Lab ile süreçler arası kilit değildir.
- `../aos/services/decider/worker.py:109`: `infer()` modeli CUDA'ya yükler; normal yanıt sonunda CPU'ya taşıma/bellek boşaltma yoktur. Çağrı çevresindeki mutex'i bırakmak GPU belleğini bırakmaz.
- `../aos/src/aos/reusable_decider.py:114`: `close()` worker process group'unu durdurup drain eder. Opt-in ortak GPU profilinde tam unload sınırı olarak kullanılabilir; yeniden yükleme maliyeti ölçülmelidir. Sürekli worker optimizasyonunun sessizce bozulmaması için profil açık olmalıdır.
- `../aos/src/aos/decision.py:44`: one-shot `DeciderEngine.decide()` alt süreç açar ve bitmesini bekler. Ortak lease için doğal sınırlı bir yürütme alanıdır.
- `../aos/src/aos/supervisor.py:117`: `BonsaiSupervisor.plan()` kendi native sunucusunu başlatır; `finally` içinde SIGTERM, bounded wait, gerekirse kendi process group'una SIGKILL uygular. Ortak lease model yükleme öncesinden bu cleanup tamamlanana kadar tutulmalıdır.
- Cancellation sırasında GPU kilidi alt süreç drain bitmeden bırakılamaz. Fencing, eski owner'ın yeni owner'a ait modeli kapatmasını engellemeli. CPU prewarm, GPU init yapmadığını doğruluyorsa lease dışında kalabilir; RAM bütçesinde yine hesaba katılır.

## vLLM uyutma seçeneği

Resmi [Sleep Mode dokümanı](https://docs.vllm.ai/en/latest/features/sleep_mode/) online sunucuda `--enable-sleep-mode` ve `VLLM_SERVER_DEV_MODE=1` ile sleep/wake yönetimini açıklıyor. Level 1 ağırlıkları CPU RAM'e taşır ve KV'yi bırakır; CPU rezervi gerekir. Level 2 ağırlık/KV'yi bırakır; uyanırken ağırlıklar yeniden yüklenir. Sleep bildirimi tek başına sıfır GPU kullanımı kanıtı değildir; bu model/quant/sürüm üzerinde NVML ölçümü gerekir. Yönetim endpoint'leri ajan araç yüzeyine veya genel kullanıcı ağına açılmaz. Qwen3.5 FP8/DeltaNet/sm_89 uyumluluğu denenmeden bu yöntem başarı olarak kaydedilemez.

Çalışabilir profil tercihi: GPU yöneticisi talep kuyruğu → bounded Qwen çağrısı → KV/weight unload doğrulaması → bekleyen AOS çağrısı. Uygulama kontrol API'si kuyruktan bağımsız yanıt verir. Level1 RAM'i host bütçesine sığmazsa kısa controlled shutdown/reload karşılaştırılır. İki farklı süreçte kilit testi, gerçek model unload ölçümünün yerine geçmez.

## Çalışan servisler

`ss -ltnp` incelemesinde AOS `127.0.0.1:8765` üzerinde açık (o anda PID 434224). `127.0.0.1:11434` üzerinde ayrı bir servis de dinliyor. Bu servislerin portu veya yaşam döngüsü Lab'ın değildir. İki uygulama portları dynamic/reserved veya ayrı önceden yapılandırılmış değerler olmalı; `8000` otomatik ele geçirilemez.

İnceleme anında `nvidia-smi` model compute process'i göstermedi; görülen compositor yaklaşık 12 MiB kullanıyordu. Bu idle ölçüm, AOS'un çalışma sırasında GPU talep etmeyeceği anlamına gelmez. GPU 42°C ve yaklaşık 13.8 W idi; model benchmark'ı yapılmadı.

## TSB-AD vendor/golden için doğrulanmış kaynak

Resmi [PyPI 1.5 metadata](https://pypi.org/pypi/TSB-AD/1.5/json) üzerinden küçük wheel indirildi ve SHA256 doğrulandı:

- Dosya: `TSB_AD-1.5-py3-none-any.whl`, 179432 bayt.
- SHA256: `4db218b330e8daf845447a714e5367d934d41b67bba4d2310dc41225d962127f`.
- Geçici yerel kopya: `/tmp/ai-scientist-review-TSB_AD-1.5-py3-none-any.whl`.
- `evaluation/basic_metrics.py` VUS için `generate_curve` içeriyor; numpy/sklearn kullanıyor.
- `evaluation/metrics.py` içindeki `get_metrics()` `pred=None` durumunda eşik bağımlı oracle yolu kullanıyor. Üretim wrapper'ı bunu engellemeli; yalnız VUS için `generate_curve` çağrılabilir.
- Ayrı numpy<2 oracle ortamı wheel'in evaluation kaynağını kullanabilir. Uygulama venv'ine tüm TSB-AD/model bağımlılıklarının kurulması gerekmez. Bağımsız beklenen değerler ve pinli oracle ortamı kaydı zorunludur.

Bu dosya 03 review'ü destekler; gerçek inference/eğitim/public-data kabulü eklemez.
