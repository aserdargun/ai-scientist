# AOS typed görev ve yetki incelemesi

Tarih: 2026-09-25 (Europe/Istanbul). Çalışma yalnız
`data/runtime/aos-coexistence/source-typed` kaynak kopyasında yürütülür.
Canlı AOS değiştirilmedi veya yeniden başlatılmadı. Önceki taşıma kabulünün
`source-current` kopyası ayrı tutulur.

## Uygulanan yol

İzole AOS kopyasında `ai_scientist` görevi, gerçek `State`, `Action`,
`ToolRegistry`, `SafetyPolicy` ve SQLite trajectory kayıtlarını kullanır.
Başlatma, deney bütçesi ve kayıtlı süitle sınırlıdır. AOS task/run/action
kimliklerini host üretir; Lab run kimliği alındıktan sonra görev yetkisine bağlanır.
Başlatma yanıtı araştırmanın başarılı olduğunu göstermez.

Testte başlatma seçimi açıkça `FixtureDecisionEngine` üzerinden yapılır.
İzinli eylem ve reddetme olmak üzere sonlu seçenekler kaydedilir. Sonraki kısa
kontrol işlemleri `lab-control-policy-v1` host policy kaydını kullanır; bunlar
gerçek model çağrısı diye gösterilmez. Durum okuma masaüstünün sürekli HUMAN
kontrolünde kalmasını gerektirmez. Araştırma DesktopScheduler'a görev eklemez.

## Bağımsız tekrar kontrolü

Komut:

```sh
data/runtime/aos-coexistence/.venv/bin/python \
  docs/ai-scientist/review-evidence/review_aos_control_retries.py
```

İlk gerçek çalıştırma iki hatayı gösterdi: ikinci stop çağrısı `AOSFault`
üretti; kalıcı typed state'i PAUSED yapılan bir isteğin tekrarı reddedilmeden
önce ikinci dış start çağrısı yapıldı. Düzeltmeden sonra **2/2, exit 0**:
ikinci stop aynı `stop_requested` sonucunu verir, yetkisi kaldırılan istekte
start çağrı sayısı 1 kalır. Önce/sonra kanıtları:

- `review-evidence/aos-control-retries-before.json`
- `review-evidence/aos-control-retries-review.json`

Bu kontrol gerçek coordinator/policy/SQLite kullanır; controller ve Lab
taşıması sahtedir. Yetki kaldırma doğrudan typed state'e uygulanmıştır.
Gerçek restart/takeover veya HTTP kabulü değildir. Geçen kaynak modülü
SHA-256: `c5cb9b10071a4de669f8f11db1658b8d0089e554b1d99b7282585a9a6ecbb2fc`.

## Gerçek typed API kontrol koşusu

Komut:

```sh
.venv/bin/python docs/ai-scientist/review-evidence/review_aos_typed_wire.py \
  --config-dir data/runtime/api-wire-review/47c46166-f307-412f-82eb-6efb29b72371
```

`review-evidence/aos-typed-control-review.json`: **12/12, process exit 0**.
Ayrı AOS Python sürecinde gerçek `TrajectoryStore`, `DesktopController`, typed
coordinator, policy ve Lab istemcisi çalıştı. Ayrı Lab HTTP servisi PostgreSQL'e
`cda48790-fce1-45d8-b7a1-b63569197eac` koşusunu ekledi. Tekrarlanan istek aynı
AOS eylemini ve Lab koşusunu kullandı; principal, üç dış kimlik ve bütçe eşleşti.

Gerçek DesktopController masaüstü sahipliğini AGENT'a geçirdi; fixture runtime
üzerindeki bir input/readback işlemini tamamladı. Bu sırada aynı arka plan
işinin durum okuması sürdü. Masaüstü runtime'ı simüledir; bu kanıt gerçek
masaüstü etkileşimi veya iki gerçek modelin birlikte ilerlemesi değildir.

Dört typed action kaydı ve sonlu kararların option/envelope/hash eşliği
denetlendi. Kuyrukta **0 deney, 0 skor**, `outcome=unknown`,
`training_eligible=0` kaldı. AOS sürecinin gerçek systemd sınırları
512 MiB RAM, 1 CPU, swap 0 ve TasksMax 32 idi. AOS temiz çıkışı 0; özel Lab
satırı ve servisler temizlendi. Kaynak hash'leri koşu boyunca değişmedi.
Bu ölçümde Lab bridge modülü SHA-256:
`42e8871f371ba3885fdfda466511fd82ff9cb28709b46b4ee95849c1006ffec2`.

İlk driver denemesi, host-policy çağrılarında da fixture deployment kimliğini
beklediği için audit aşamasında durdu. Önceki dokuz kontrol geçti; kaynaklar
sabit kaldı ve özel kaynaklar temizlendi. Bu driver varsayımı düzeltildi;
`aos-typed-control-before.json` başarısız sonucu korur.

## Tam typed araştırma ve rapor

Aynı driver `--execute` ile çalıştırıldı:

```sh
.venv/bin/python docs/ai-scientist/review-evidence/review_aos_typed_wire.py \
  --config-dir data/runtime/api-wire-review/47c46166-f307-412f-82eb-6efb29b72371 \
  --execute
```

`aos-typed-director-review.json`: **22/22, exit 0**. Koşu
`efc64e2a-3262-4834-bfe2-2916cb73fd43`, üç baseline ve bir KEEP adayla
**48 bağımsız Scorer skoru**, dört fiziksel hash-doğrulamalı belge çifti ve
kapalı görev planı üretti. Referee kararı ham Scorer sonuçlarından bit eşliğiyle
yeniden kuruldu. Nihai rapor SHA-256:
`f1af9ac304347278f0466c8a7bc8006f8d9972ff968021f3414645650ce055f3`.

AOS tarafında 37 typed action (`lab.start/status/report`) ve eşleşen bağımsız
verification kaydı denetlendi. Görev, doğrulama sonrasında `SUCCEEDED/passed`
oldu; `training_eligible=0` kaldı. Başlatma FixtureDecisionEngine, devam eden
kontroller host policy kullandı. Araştırma boyunca masaüstü sahibi AGENT idi;
masaüstü runtime sınırı fixture olduğundan gerçek etkileşimli masaüstü sonucu
iddia edilmez.

603,59 saniyeye yayılan 302 Lab health/status örneğinde status en yüksek
2,13 ms, health 1,31 ms idi. Ayrı typed AOS yolundan 32 status örneği alındı.
Örneklenen MemoryPeak: AOS 29.990.912 bayt, API 161.144.832 bayt, Director
207.417.344 bayt. Bunlar üç özel birimin ölçümleridir; sandbox/Scorer/DB veya
tüm host tepesini kapsamaz. Birimlerin sınırları sırasıyla 512 MiB/1 GiB/2 GiB,
birer CPU ve swap 0 idi.

Director ve AOS süreçleri exit 0 verdi; yalnız özel koşunun SQL satırları ve
servisleri temizlendi, kanıt dosyaları korundu. Kaynaklar ve Lab 0.14.0
harness/imajı sabitti. **M0.AOS.1 bu sentetik ve açık fixture karar motoru
kapsamında geçti.** Gerçek AOS/Qwen modelleri ve GPU birlikte kullanım kapıları
açık kalır.

## Paket ve yama

Son kaynak sürümünde bağımsız olarak şu komutlar çalıştırıldı:

```sh
# data/runtime/aos-coexistence/source-typed içinde
PYTHONPATH=src ../.venv/bin/python -m pytest \
  tests/test_dataset_audit.py tests/test_lab_external_jobs.py -q
../.venv/bin/python scripts/validate_package.py
```

İlk komut **32 passed, exit 0**; ikincisi **3842 package checks, exit 0**.
Tam çıktılar `aos-typed-gate-0.txt` ve `aos-typed-gate-1.txt`, kaynak ve log
hash'leri `aos-typed-package-gate.json` içindedir. Migration 0019 audit
kataloğuna eklendi. Testler stop hatasının kalıcı kaydını, gerçek console route'u
üzerinden kontrol devrinin sürdüğünü ve typed run'ın PAUSED olduğunu da sınar.
Controller/Lab taşıması bu negatif testlerde fixture'dır. Tam AOS pytest
paketinin geçtiği iddia edilmez.

`review_aos_typed_patch.py` on bir dosyayı geçici Git dizininde uyguladı ve
byte eşliğini doğruladı. Teslim `aos-lab-typed-policy.patch`, SHA-256:
`55c10428f40e010ea43002e13eac1e3a222b8a51835cd717905ee320882e5011`.
`aos-lab-typed-policy.json` önceki/sonraki dosya hash'lerini tutar. Son Lab bridge
modülü `6b02e7bb…`; kaynaklar paket ve yama kontrolleri sırasında sabitti.

Bu yama **tam olarak kayıtlı `source-current` test kopyasını** hedefler.
Onun validation manifest'i (`6b862b38…`), önceki API mapping yamasının teslim
manifest'inden farklıdır. İki patch kör biçimde zincirlenmez. Canlı AOS'a
aktarım, paralel geliştirmenin güncel dosyalarıyla yeniden tabanlama ve yeni
doğrulama gerektirir. Kamu kaynak kopyasındaki eksik `services/laya/worker.py`
de bu test kopyasının paketine eklenmiştir; canlı dosya değiştirilmemiştir.

## Açık işler

- Gerçek HTTP/in-flight pause/takeover/drain ve süreç restart uzlaştırması.
  Hedefli fixture kontrolleri bu kabulün tamamını kapatmaz. Takeover sorgusu
  en çok dokuz işi ele alıyor; sekiz işlik admission tavanı uygulanmadığından
  bu sınırın üzerindeki işler için revokasyon kapsamı açık. Yanıtı kaybolan ve
  henüz Lab handle'ı bilinmeyen başlatmanın uzlaştırması da açık.
- Gerçek Decider/Qwen, public veri, GPU kaynak devri ve bütün host birlikte
  kullanım ölçümleri. Bu dosyadaki fixture sonuçları bu kabulleri kapatmaz.

Lab 0.14.0 üretim kodu bu dilimde değiştirilmedi. Teslim öncesi tam Lab kalite
kapısı yeniden çalıştırıldı: **176 passed/11 deselected**, strict mypy 63 kaynak,
yedi komut ve üst süreç **exit 0**. Değişmez kayıt
`evidence/quality-gate-aos-typed.json`; SHA-256
`65324fdd743d75798c54736c639b8a37c9e42d578af9da92530beb3352ce9678`.
Kaynak/harness/imaj eşliği `aos-typed-lab-quality-gate-binding.json` içindedir.

İlk gate çağrısında özel systemd biriminin PATH ayarı `uv` dizinini içermediği
için wheel adımı `FileNotFoundError` ile durdu; başarısız exit 1
`aos-typed-lab-gate-before.json` içinde saklandı. PATH düzeltildikten sonra
tam kapı yeniden çalıştırıldı. Yeni AOS kaynaklarının package gate'i bu Lab
sonucundan ayrıdır.
