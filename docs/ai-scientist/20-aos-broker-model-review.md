# AOS GPU aracısı — gerçek model incelemesi

2026-09-25. Bu dilimde canlı `/home/cachyos/aos` kaynağı veya oturumu
değiştirilmedi. İzole AOS kaynak kopyası, ayrı AOS Python ortamı ve iki
geçici systemd kontrol servisi kullanıldı. Görev girdileri sentetiktir;
Decider ve Bonsai çağrıları gerçek yerel modellerle yapıldı. Modelin önerdiği
dosya işlemleri yürütülmedi.

## Düzeltilen sorunlar

- Başarılı tek çağrılı işçinin doğal çıkışı, MainPID/InvocationID kontrolü
  yüzünden reddedilebiliyordu. Bağlanmış süreç kimliği ile boş cgroup kontrolü
  terminal durumda ayrı doğrulanıyor.
- Yükleme/çıkarım/toplam süre sınırları `600/540/720` olarak tutarsızdı.
  Kuyruk üst sınırları `600/120/720`; test profilleri ayrıca `180/60/240`
  saniyeyle sınırlandı. İstemcinin monotonic son tarihi scheduler'ın boottime
  saatine kalan süre olarak aktarılıyor.
- İşçi, model hazır olduktan sonra nonce ile bağlı izin dosyasını bekliyor;
  scheduler çıkarım aşamasını kabul etmeden model çağrısı başlamıyor.
- Lab ve AOS'un kendi bağlama tablolarında kayıt bulamaması, diğer tarafın
  GPU sürecinin bittiğini kanıtlamaz. Her iki drain callback'i desteklemediği
  sahip için kapalı kalıyor. Son kaynakların 0.19 kalite/imaj bağı aşağıdadır.
- Bonsai yüklenirken gelen HTTP 503 sınırlı beklemeye alındı. Manifestteki
  16 `.so` bağlantısının paket içindeki, yine manifestte kayıtlı hedeflerine
  gitmesine hash doğrulamasıyla izin verildi; dışarı çıkan veya kopuk hedefler
  kabul edilmiyor. Model dosyaları ve manifestler değiştirilmedi.
- Başarısız işçinin sınırlı hata özeti/stderr kaydı, çalışma dizini
  temizlenmeden önce saklanıyor. Decider model yükleme süresi ve broker süreç
  kimliği son metriklerde korunuyor.

## Gerçek süreç kontrolü, CPU fixture

`review-evidence/review_aos_broker_lifecycle.py` üretim SQLite scheduler'ını,
gerçek systemd başlatma/kaynak sınırlarını, özel ağ alanını, hazır/izin
geçişini ve süreç temizliğini çalıştırır. Model komutu CPU fixture'dır;
GPU gözlemcisi ve peer kimlik sağlayıcısı da açıkça fixture'dır.

İlk komut süre sınırları nedeniyle **exit 1** verdi; model veya işçi
başlatılmadı (`aos-broker-lifecycle-cpu-before.json`). Düzeltme sonrası
**session 11947 / exit 0, 11/11 kontrol, 1,08 saniye**:
başarılı sonuç `completed`, çıktısız hata `failed`, iki cgroup boş.
Kanıt: `aos-broker-lifecycle-cpu-after.json`.

## Gerçek Decider ve Bonsai

Komut:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python \
  docs/ai-scientist/review-evidence/review_aos_broker_models.py \
  --output docs/ai-scientist/review-evidence/aos-broker-real-model-after.json \
  --execute
```

**Session 38880 / gerçek exit 0; 8/8 kontrol; 24,19 saniye.**
Gerçek `ReusableDeciderEngine` ve `BonsaiSupervisor`, doğrulanmış UDS peer
kimliğiyle aynı üretim scheduler'ına başvurdu. Her çağrı ayrı model
systemd servisi, ağ alanı ve cgroup içinde yürüdü; iki yanıt AOS'un typed
doğrulamasından geçti. Bonsai kanıt referansını ve izin verilen üç adımlı
planı doğruladı. İki kuyruk kaydı `done`, iki sonuç `completed`, iki
model bağlaması `drained` oldu.

| Ölçüm | Decider | Bonsai recovery |
|---|---:|---:|
| AOS çağrısı uçtan uca | 7,03 sn | 16,66 sn |
| Giriş token | 88 | 159 |
| Çıkış token | Sonlu seçenek kararı | 242 |
| Modelin bildirdiği çıkarım | 0,628 sn | Ayrı ölçülmedi |
| Gözlenen cgroup bellek zirvesi | 1.598.001.152 byte | 6.986.883.072 byte |

45 dış gözlem örneğinde GPU toplamı **62 → 8816 → 62 MiB**.
Model servislerinde 10 GiB RAM, swap 0, %200 CPU ve 96 task sınırı;
ortak GPU slice'ında 16 GiB/%200 sınırı uygulandı. Cgroup örnekleri ham
kanıtta tutulur; bunlar bütün host'un veya eşzamanlı Lab araştırmasının
kapasite kabulü değildir. Decider'ın kendi Torch zirvesi 3.811.927.040 byte.

Kaynak hash'leri test boyunca sabit kaldı. Son kontrolde bütün test servisleri
yoktu, model cgroup'ları boştu ve compute GPU tüketicisi kalmamıştı. Özel
scheduler DB'si byte doğrulamasıyla test artefakt dizinine arşivlendi;
yalnız bu testin sahip olduğu durdurulmuş socket/dizin temizlendi.

Önceki gerçek deneme **session 6614 / exit 1**: Decider başarılı, Bonsai
kütüphane bağlantısı doğrulamasında başarısız. İki süreç de temizlendi.
`aos-broker-real-model-before.json`, `aos-bonsai-adapter-pin-review.json`
(CPU teşhisi, session 45890 / exit 1) ve
`aos-bonsai-runtime-alias-review.json` başarısızlığı ve nedenini korur.

## Birleşik gerçek Qwen/AOS denemesi

Önceki bölümdeki iki çağrı da AOS sahibine aittir. Aşağıdaki ayrı deneme,
kalıcı `lab.llm.aos_gpu_service` paketi ve aynı SQLite scheduler üzerinden
iki gerçek Qwen tanı çağrısını AOS Decider/Bonsai çağrılarıyla sıraya aldı.
Her iki kontrol servisi de `swapp-gpu.slice` içindeydi.

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python \
  docs/ai-scientist/review-evidence/review_aos_broker_models.py \
  --output docs/ai-scientist/review-evidence/aos-lab-real-gpu-coexistence-after.json \
  --execute --production-broker --with-qwen
```

**Session 74499 / gerçek exit 1; 217,40 saniye; 10/11 kontrol.** Gerçek
model nesilleri **Lab → AOS → Lab → AOS** sırasıyla ilerledi. İkinci Lab
isteği önce kuyruğa girmiş olmasına rağmen sıradaki AOS isteği öne alındı;
böylece dönüşümlü sıra gerçek modellerde gözlendi. Dört kuyruk kaydı `done`,
iki AOS sonucu `completed`, model süreçleri `drained` oldu.

| Çağrı | Kuyruk dahil uçtan uca | Sonuç |
|---|---:|---|
| Qwen S1 tanı | 100,87 sn | Doğru `2` yanıtı; 42 giriş/2 çıkış token |
| AOS Decider | 76,38 sn | Geçerli typed karar |
| Qwen S2 tanı | 188,98 sn | 512 tokenlık çıktı sınırı aşıldı; yanıt reddedildi |
| AOS Bonsai recovery | 108,38 sn | Geçerli typed recovery planı |

Çağrılar farklı anlarda kuyruğa girdiği için süreler toplanmaz. S1 model
başlangıcı 85,56 sn, çıkarımı 0,384 sn, drain 1,245 sn sürdü. Gözlenen GPU
zirvesi **12756 MiB**, son değer **62 MiB**; S1 model cgroup zirvesi 10 GiB.
Bu kısa tanı denemesi tüm host için OOM yokluğu veya interaktif gecikme
kabulü vermez. S2 başarısızlığından sonra süreç temizlendi ve Bonsai ilerledi.

`aos-lab-real-gpu-coexistence-after.json` ham sıra/ölçüm/sonuçları korur.
`aos-lab-gpu-test-state-archive.json`, yalnız bu denemenin sahip olduğu
servisler durduktan ve dört ticket kapandıktan sonra DB/socket durumunun
byte doğrulamalı arşivini kaydeder. Canlı AOS ve dış süreçler değiştirilmedi.

İlk birleşik Qwen/AOS denemesi model yüklemeden **exit 1** verdi:
ön kontrolde kuyruk dışındaki Python PID 2705953 yaklaşık 3944 MiB GPU
belleği kullanıyordu. Test servisi/DB/modeli başlatılmadı. Salt okunur takipte
aynı PID'nin `/proc` kaydı ve GPU girdisi kendiliğinden kayboldu; GPU 62 MiB'a
döndü. Uygulama kimliği AOS olarak doğrulanmadı; dış sürece sinyal gönderilmedi.
Kanıtlar `aos-lab-real-gpu-coexistence-before.json` ve
`aos-lab-preflight-external-gpu-review.json`. Bu ön kontrol birlikte çalışma
kabulü değildir; yukarıdaki deneme ancak GPU yeniden boş gözlendikten sonra
başlatıldı.

## Son kaynak, imaj ve kalite bağı

Broker üç kalıcı `lab/llm/aos_gpu_*.py` modülü, `lab-aos-gpu-broker`
entrypoint'i ve `ops/systemd/swapp-lab-gpu-broker.service` ile teslim edilir.
**Harness 0.19.0**: 231 test, strict mypy 69 kaynak, Pylint 9.32/10;
yedi kalite komutu **session 9552 / exit 0**. İmaj build/parity
**session 10252 / exit 0**, 77 runtime dosyasında byte eşliği.
İmaj: `sha256:709275c8063e9496fd6f742a292a204cf1be1484fd784544fafed8104c2cbb2c`.
Kaynak/imaj/kalite ve birleşik denemenin aynı altı üretim modülünü kullandığı
`aos-gpu-broker-019-quality-gate-binding.json` ile doğrulandı. Binding'in
başarısı S2'nin başarısız sonucunu değiştirmez.

Projenin editable console entrypoint'i bağımlılık çözümlemeden yenilendi;
`systemd-analyze --user verify` exit 0 (`aos-gpu-broker-entrypoint-review.json`).
Bu statik kontrolde servis başlatılmadı. Opt-in profil yapılandırması,
normal AOS bootstrap ve canlı kaynaklara koordineli aktarım ayrıca gereklidir.

## AOS yamasının teslimi

`review-evidence/aos-lab-gpu-broker.patch`, kayıtlı canlı kaynak görüntüsünü
hedefleyen **24 dosyalık** tam deltadır; önceki yamalarla zincirlenmez.
Baseline manifest SHA `fb0c38f2…`, yama SHA `ccfebffc…`.
İki broker worker, Decider GPU hazırlığı/metric düzeltmeleri ve typed Lab
entegrasyonu dahil; yalnız izole test kopyasının manifesti yenilendi.

Yama bağımsız kopyaya uygulandı; değişen ve değişmeyen bütün kayıtlı
dosyalarda byte eşliği geçti. Hedefli AOS testleri **57 geçti / 9 tarayıcı
testi atlandı**, paket kontrolü **4100**, gerçek exit 0.
`aos-lab-gpu-broker-patch-review.json` komutları ve hash'leri tutar;
eski worker manifest hash'leriyle oluşan ilk exit 1 ayrı `-before.json`
kaydında korunur. Gerçek birleşik tanının kullandığı **307 AOS Python
dosyası**, teslim yaması/baseline birleşimiyle aynı byte'lara sahiptir.
Canlı AOS'ta paralel değişiklikler olabileceği için aktarım öncesi yeniden
uzlaştırma gerekir; bu yama canlı oturuma uygulanmadı.

## 0.20 ile başarılı dört model tanısı

`7b9b191` ürün kaynaklarında aynı komut yeni
`aos-qwen-020-broker-model-review.json` hedefine çalıştırıldı:
**session 52269 / gerçek exit 0, 12/12 kontrol, 211,187 saniye**.
Güncel S2 tanı profili `top_k=20` kullanır. Gerçek model nesilleri sırasıyla
**Qwen S1 → AOS Decider → Qwen S2 → AOS Bonsai** oldu. S1 `2`, S2 geçerli
`{"answer":2}` döndürdü; S2 44 giriş/140 çıkış token kullandı. Her iki AOS
yanıtı da kendi typed doğrulamasından geçti. Dört ticket `done` ve bütün
test/model servisleri terminal; son compute GPU tüketicisi yok.

| Dış gözlem ölçümü | Değer |
| --- | ---: |
| Toplam GPU bellek zirvesi | 12754 MiB |
| Son GPU belleği | 62 MiB |
| En düşük kullanılabilir host RAM | 19.787.816.960 bayt |
| En düşük boş disk | 81.158.430.720 bayt |
| En yüksek GPU sıcaklığı | 71 °C |

Gözlemci artık her örnekte RAM, disk ve sıcaklığı kaydeder; 6 GiB RAM,
20 GiB disk rezervi ve 83 °C sıcaklık sınırı korundu. Cgroup kaynak
sınırları ham kanıtta bulunur. Qwen çağrı süreleri kuyruk beklemesini de
içerir; bu süreler tek başına çıkarım gecikmesi değildir.

Ürün fingerprint'i 0.20'nin 239 testli, yedi komutlu başarılı kalite
kapısıyla aynıdır. `aos-qwen-020-broker-model-outcome.json` gerçek tool exit
code'unu, kaynak/kanıt hash'lerini ve özel scheduler DB arşivinin byte
eşliğini kaydeder. Yalnız bu teste ait kapanmış socket/owner marker
temizlendi; kalıcı durum test dizinine taşındı. Canlı AOS değişmedi.

Girdiler aritmetik ve sentetik AOS görevleridir; modelin önerdiği dosya
işlemleri çalıştırılmadı. Bu başarı gerçek araştırma önerisi, Scorer,
interaktif desktop veya kesinti/kurtarma kabulünü kapatmaz.

## Açık kapsam

**Başarılı gerçek S2 araştırma önerisi, altı önerilik araştırma, public veri, vision, tam desktop
akışı, stop/takeover/restart, eğitim ve toplam host bütçesi kabulü açık.**
Birleşik tanı denemesi iki sahip arasında gerçek sıra devrini ve temizliği
gösterdi; tam araştırma/desktop kabulü değildir. M0 kabul sayıları değişmedi:
**3 geçti / 13 kısmi / 6 açık**.
