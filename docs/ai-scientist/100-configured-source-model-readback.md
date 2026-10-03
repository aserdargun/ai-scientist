# Native kaynak bağlantısı ve kurulu model doğrulaması

2026-10-01. Scientist tabanı `f13b0117be78efd8e86ea50560ebc5082927df4f`;
AOS `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` ve incelenen yerel kaynakları.

## Somut ilerleme

`ConfiguredScientistAdmissionFactory` AOS'un kendi configured-source verifier
ve bootstrap factory sınıflarını birleştirir. Ayrı writer veya GPU allocator
kurmaz. Üç profil, enabled policy, bağımsız source/config hash'leri, output
sözleşmesi ve güncel broker/caller generation denetlenir. Artifact/runtime
doğrulayıcısı verilmeden constructor reddeder. Native worker'ın model aktivasyonu
öncesindeki artifact hash ve dependency-version kapıları korunur.

Gerçek AOS `serve_desktop.main` bağlantısı dört hook gerektirir:

```python
serve_desktop.main(
    scientist_admission_factory=factory,
    scientist_bootstrap_expected_peer=factory.expected_peer,
    scientist_confirm_runtime=factory.confirm_runtime,
    scientist_output_contract=factory.output_contract,
)
```

Wrapper, exact native type isteyen `scientist_bootstrap_factory` kısa yoluna
verilemez. Review eksik output-contract hook'unu ve Unicode manifest deployment
hash uyumsuzluğunu yakaladı; ikisi düzeltildi. Actual AOS interpreter native API'yi
import etti; model veya torch import edilmedi. Native60 kaynak profili ve strict
source pin zinciri eklendi; admission bu salt okunur preflight ile açılmaz.

## Gerçek dosya/ortam kontrolü ve düzeltilen hata

`check_aos_model_environment.py` bağımsız hash'i verilen private receipt ile
AOS'un mevcut `verify_environment`, `verify_manifest`, `verify_projection_pin`
fonksiyonlarını çalıştırır. Model oluşturmaz, servise/GPU'ya tahsis istemez.
Bu doğrulama CPU100%/RAM1GiB/swap0/tasks64/runtime600s ile ayrı Scientist unit'inde,
CUDA görünürlüğü kapalı ve AOS mount'u salt okunur olarak yapıldı.

İlk deneme AOS uygulama `.venv` ortamıyla **exit1** verdi: Decider dependency
environment manifest ile uyuşmadı. AOS belgeleri ve önceki doğrulanmış profil,
model interpreter'ının `/home/cachyos/.venv/bin/python` olduğunu gösterdi.
Doğru mevcut model ortamındaki ikinci deneme **exit0 / 8,300048067 saniye** verdi:

- Decider: 7 model ve 4 kod dosyasının hash'leri; 197 tam dependency sürüm kümesi.
- Bonsai: 2 model, 84 runtime dosyası ve 20 native library hash'i.
- Kaynak ve manifest byte'ları önce/sonra aynı; unit toplandı, MainPID0.

Bonsai manifest'inde projection pin yok; `bonsai_projection_verified=false`
ayrı kaydedildi. Bu kontrol output-v2 gerçek model cevabını veya tam dependency
byte kapanışını ispatlamaz. Model yüklenmedi; VRAM tepe veya model gecikmesi ölçülmedi.

Konfigürasyon hazırlayıcısına zorunlu `--decider-python` eklendi; uygulama/model
ortamları artık tek interpreter'a zorlanmaz. Native58/59/60 receipt kabul edilir.
Actual manifestler ve native60 source receipt ile üç profilli **disabled** plan
üretildi, exit0. Policy etkinleştirilmedi; DB/scheduler/broker/GPU başlatılmadı.

## Paralel AOS kaynak değişikliği

Sonraki config denemesi eski receipt'i reddetti. Değişen `scientist_protocol.py`
ve `scientist_lab.py` güncel kaynakları ayrıca incelendi: `purpose` alanı Scientist
API ile eşleşiyor, baseline araştırma sonucu olarak benimsenmiyor. Güncel exact
pinlerle disabled plan üretildi. Eski receipt/policy yeni koşuyu yetkilendiremez.
Bu inceleme güncel uyumluluk incelemesidir; eski byte snapshot olmadığından tam
tarihsel diff olduğu iddia edilmez.

## Geçen / kalan / çalıştırılmayan

- **Geçen:** 137 odaklı kontrol, exit0/9,264257982 saniye; Python kaynakları sabit.
  Gerçek native API importu, kurulu artifact/native dependency kontrolleri ve
  güncel native60 disabled config üretimi geçti.
- **Kalan:** current full broker/caller binding'lerinin bağımsız hazırlanması;
  bounded native doğrulama sağlayıcısının enabled policy/canonical launcher'a
  bağlanması; tek mevcut scheduler rezervasyonu ve kullanıcı işi çakışma kontrolü.
- **Çalıştırılmayan:** gerçek AOS model görevi → GPU devri → Scientist S1/S2 deney
  → bağımsız Scorer → AOS sonuç doğrulaması; ayrıca allocated iptal/toparlanma,
  birlikte yük/fairness ve eğitim. Artifact okuması gerçek model kabulü değildir.

Ana kabul **11 geçti / 7 kısmi / 4 açık**. AOS dosyaları, canlı uygulamalar ve diğer
Codex süreçleri değiştirilmedi. Push/merge/deploy yapılmadı. Lisans ve genel CI
ayrı maddelerdir.

## AOS oturumuna kısa aktarım

Scientist native60 kaynak verifier + native factory/provider sözleşmesini tanıyor.
Actual AOS `purpose`/baseline-rejection güncellemesi API ile uyumlu. Desktop
bağlantısı dört explicit hook gerektiriyor. Model Python'u AOS uygulama Python'undan
ayrı tutulmalı. Current source/config pinleri tam binding'lere yeniden işlenmeli;
source-ready veya disabled plan runtime admission değildir. Ortak GPU koşusunu
yalnız Scientist yürütür; stop ACK/idle GPU release kanıtı değildir.

[Commit, fark, kaynak, model ayarı ve kontrol kanıtları](review-evidence/configured-source-model-readback.json).

Zorunlu kalite kapısı **2024 passed / 7 opt-in skipped / 120 GPU-live deselected**;
yedi komut exit0, parent187,348076309 saniye. Python kaynakları kapı boyunca
değişmedi; wheel build/import geçti. Kalite sonucu gerçek GPU kabulünün yerine
geçmez.
