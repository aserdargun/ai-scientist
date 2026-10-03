# AOS CPU deney sözleşmesi — v0.1.0 sonrası aday

Durum: izole kaynak adayında gerçek, kontrollü AOS → Scientist CPU çağrısı
tamamlandı. API/PG/Director/Scorer ve AOS rapor doğrulaması sentetik veriyle
ölçüldü; karar/onay girdileri fixture idi. [Koşu ve kabul sınırları](125-aos-controlled-cpu-integration.md).
Canlı field-lab 0.46.0 ve v0.1.0 etiketi değişmedi; aday harness 0.47.0 bu
profile yüklenmedi. İşlem hattı sınırında stop/tekrar/recovery ve izole
API/PG cleanup geçti; Scorer aktifken stop kabulü doğrulanmadı.

## Kullanım

AOS, önceden hazırlanmış sentetik veri üzerinde kayıtlı LSH/OPTICS/SOM
ayarlarını bütçeli bir deney olarak başlatabilir. Bu yol model çağırmaz.
Özel veritabanı veya public DEV veri yetkisini devralmaz; bu kaynakların AOS'a
açılması ayrıca veri sahibi ve uygulama yetkisi gerektirir.

Operatör önce mevcut sentetik snapshot'ı normal Scorer kurulumundan geçirir ve
yerel grid oluşturur. Sonra ayrı bir AOS sahibi için açık grant kaydeder:

```bash
PYTHONPATH=. .venv/bin/python scripts/register_aos_cpu_study.py \
  --runtime-root /path/to/isolated/data/runtime \
  --registry /path/to/isolated/data/runtime/registry.json \
  --source-suite EXISTING_SYNTHETIC_GRID_SUITE \
  --owner-id AOS_CONFIGURED_OWNER \
  --max-experiments 1 --max-wall-seconds 600
```

Komut mevcut yerel suite'i değiştirmez; grant hash'inden yeni suite kimliği
üretir. Sadece kayıt yapar; principal oluşturmaz, servis/model/deney başlatmaz.
Mevcut kayıtlı ayarların sınırlı bir öneki çalıştırılabilir. Kaynak, grid,
Scorer kurulum kaydı ve grant eşleşmeden kayıt yayımlanmaz.

## Wire

`GET /v1/aos-cpu-capability/{suite_id}` mevcut Bearer principal çözümleyicisini
kullanır. Yalnız aynı `origin=aos` ve `owner_id` için kayıtlı grant görünür.
Sözleşme: `scientist.lab-cpu-capability.v1`. Mevcut
`/v1/aos-capability/{suite_id}` / `scientist.lab-capability.v1` yerel model
sözleşmesi değişmez. Şema dosyası: [JSON Schema](../contracts/aos-cpu-capability.v1.schema.json).
[Örnek yanıt](../contracts/aos-cpu-capability.v1.example.json) tamamen kurgusal
kimlik/hash içerir; canlı kayıt veya kabul kanıtı değildir.

Yanıt kimliği, suite/manifest/entry/config/snapshot/grant hash'lerini,
`max_experiments`, `max_wall_seconds`, `model_tokens=0` ve
`allowed_purpose=research` alanlarını taşır. `source_kind=synthetic`,
`provider=mode-grid`, `track=mode`, `program_version=mode-grid.v1`.
`allocation_authority`, `gpu_release_verified`, `native_inference_authorized`
ve `launch_authorized` kesinlikle `false` olur. Yanıt başlatma onayı değildir.

Hash kanoniği: UTF-8, `ensure_ascii=False`, anahtarlar sıralı,
`separators=(',', ':')`, `allow_nan=False`. Grant hash'i tüm typed grant
alanlarını kapsar; entry hash'i granta bağlanır. Eski, grant içermeyen entry
serileştirmeleri ve hash'leri korunur.

Başlatma mevcut `POST /v1/runs` yolundadır. AOS task/run/action kimlikleri ve
normal AOS policy/approval şartları devam eder. Deney limiti, duvar süresi ve
sıfır token kısıtı API girişinde doğrulanır. Grant ve hash'i kalıcı isteğe
eklenir; idempotency bunları da kapsar. İşçi başlangıcı ve takeover aynı
kaydı ve owner bağını yeniden doğrular. Sıradan yerel grid kaydı AOS için
yeterli değildir; grant alanı silinerek eski yürütme yoluna dönülemez.

`field_intent` Scientist tarafında mevcut typed sözleşmeyle taşınır ve veri
snapshot'ına bağlanır. AOS web/DTO aktarımı için karşı projede geliştirme
gerekiyor. Varlık adı kullanıcı beyanıdır, ekipman kimliği doğrulaması değildir.

## Kabul sınırları

- Kaynak adayı ve ilgili eski akışlarda **155 passed**, 81 deprecation uyarısı;
  strict mypy (4 kaynak), Ruff, Bandit ve diff kontrolleri başarılı.
  [Kesin kaynak ve inceleme kaydı](review-evidence/aos-cpu-source-candidate-20261003.json).
  Son tam kalite kapısı **exit 0**: **3532 passed / 49 skipped / 177 deselected**;
  strict mypy, wheel build/import ve diğer kapılar başarılı. Aday commit/push/deploy edilmedi.
  [Tam kapı özeti](review-evidence/aos-cpu-quality-gate-20261003.json).
- Kaynak testleri API → kalıcı kuyruk/idempotency ve worker yeniden doğrulamasını
  izole SQLite/sentetik fixture ile kapsar; gerçek AOS runtime kabulü değildir.
- Gerçek kontrollü normal CPU koşusu `aa7d3d6f-e44e-4a88-9b53-6fa46f484e0a`
  tamamlandı; bağımsız Scorer raporu AOS tarafından doğrulanıp journal'a
  kaydedildi. [125 numaralı kanıt özeti](125-aos-controlled-cpu-integration.md)
  kapsamı ve açık kabul maddelerini tutar. Otonom model, web UI ve GPU kabulü
  bu koşuyla tamamlanmış sayılmaz.
- GPU tahsis, native dışlama, fair-turn ve release/cleanup protokolleri bu CPU
  sözleşmesiyle değişmez; gerçek GPU birlikte çalışma hâlâ açıktır.
- v0.1.0 etiketi ve çalışan 0.46.0 profili bu kaynak adayıyla değişmez.
