# Public DEV verisinde yerel deney eylemcisi

## Amaç ve sınır

Seçilmiş, değişmez public DEV snapshot üzerinde yerel modelin LSH, OPTICS
ve SOM parametreleri önermesi; mevcut Director ve bağımsız Scorer ile ölçüm
alması. Manuel CPU grid izni bu araştırma iznine dönüştürülmez. Öğretmen model
bu yolun önkoşulu değildir.

Yeni kaynak yolu `PublicDevAgentStudy` ile ayrıca kayıt edilir: kullanıcı,
kaynak/split bağları, sağlayıcı yapılandırması ve profil, öneri/süre/token
bütçeleri sabittir. Arayüz yalnız aynı kullanıcı için doğrulanmış kayıt
sınırlarını gösterir. `model_runs_enabled` ve mevcut ortak GPU scheduler
kabulü ayrıca gerekir. Kayıt yapmak model başlatmaz.

## Verinin doğru anlatılması

Public snapshot fit bölümü kaynak sırasından, etiketlere bakılmadan alınır.
Normal/sağlıklı eğitim verisi garantisi yoktur. Public yerel araştırma prompt'u
bunu açıkça belirtir ve ayrı sağlayıcı yapılandırma hash'i taşır. Eski sentetik
profil hashleri ve CPU kayıtlarının anlamı korunur. Modelin önerdiği yapılandırma
mevcut derleyiciyle adaya çevrilir; ham seri, etiket veya holdout modele verilmez.

## Teslim durumu

Bu ekin kaynak ve arayüzü, kullanıcının CPU profili güncelleme onayıyla canlı
`field-lab` kurulumuna uygulandı (iç harness 0.47.0). Kurulu public CPU izni
korundu; ayrı public yerel eylemci izni kurulmadı ve model çağrıları kapalı kaldı.
v0.1.0 etiketi değişmedi. [Dağıtım ve canlı kontrol kanıtı](129-runtime-package-preflight.md).
Yeni araştırma için model/GPU koşusu yapılmadı; kaynak ve tarayıcı kanıtı
başarılı yerel model araştırması sayılmaz.

Arayüz önizlemesinde izin yok, model kapalı, izin sınırları içinde ve bozuk
izin yanıtı durumları doğrulandı. Sınır aşımı başlatmayı kapatır; profil kayıttaki
seçime bağlıdır. Hiçbir başlatma isteği veya model çağrısı gönderilmedi.
Backend odaklı doğrulama: 153 test geçti; gerçek, izole PostgreSQL gerektiren
bir test bu CPU çalışmasında atlandı. Ruff, strict mypy ve scoped Pylint exit 0.
Bağımsız incelemede bulunan eski izni yeniden yayımlama açığı hem public agent
hem public CPU API yolunda düzeltildi. Dört eski sağlayıcı yapılandırma hash'i
v0.1.0 kaynak sürümüyle byte karşılaştırmasıyla aynı kaldı.

Tarayıcı önizlemesi, iki bozuk izin şekli dahil beş durumu doğruladı; bunlar
mock izin/model hazır bilgisi kullandı, gerçek model çalışması değildir.
Yeni kaynağın tam proje kalite kapısı tamamlandı: **3595 passed, 49 skipped,
177 deselected**; yedi komut exit 0. İlk koşudaki Bandit assert uyarısı
normal koşulla düzeltildi; son kaynakta kapının tamamı yeniden geçti.
[Kaynak teslim kanıtı](review-evidence/public-dev-local-agent-source-20261003.json).

## Operatör kaydı

Önkoşul: kaynak/split/lisans bağları doğrulanmış public DEV snapshot ve mevcut
bağımsız Scorer kurulumu. Bu komut veri indirmez veya yeni split üretmez.
Aşağıdaki örnek yalnız taslağı gösterir; değerleri kurulumunuzun kayıtlarıyla
ve kabul edilmiş bütçeyle doldurun:

```sh
PYTHONPATH=. .venv/bin/python scripts/register_public_mode_agent.py \
  --runtime-root /path/to/private/runtime \
  --registry /path/to/private/suite-registry.json \
  --snapshot SNAPSHOT_SHA256 --owner OWNER_ID \
  --profile-set smoke --experiments 1 --wall-seconds 600 --model-tokens 4000
```

İncelenen taslağın kaydı için aynı komuta `--register` eklenir. Registry değişir;
çalışan API'nin kayıt yükleme davranışına göre kontrollü yeniden yükleme gerekir.
Bu kayıt, AOS/native başlatma onayı veya GPU rezervasyonu değildir. Arayüzün
`local_agent_study` yanıtı boşsa kayıt yoktur veya bu kullanıcı için uygun
değildir. Kaynak veya sağlayıcı hash'i değişirse eski kayıt yeniden kullanılamaz.

Gerçek araştırma, **Yerel eylemciyle deney tasarla → kurulu veri kaynağı →
Yerel deney eylemcisi** yolunda bu kayıt sınırlarıyla başlar. Model çalıştırma
profili ve ortak kaynak kabulü ayrıca açık ve doğrulanmış olmalıdır.

## Kayıt kaldırma ve çalışan koşu

Mevcut registry şemasında aynı policy'yi taşıyan türetilmiş suite kayıtları
ayrı yetkili kayıtlardır. Bir policy'yi tamamen kaldırmak için onunla eşleşen
**tüm** suite kayıtları kaldırılmalıdır; yalnız ilk kayıt satırını silmek diğer
kayıtları kaldırmaz. Çakışan policy sınırları reddedilir. Yeni API yayını, kayıt
kilidi içinde diskteki güncel izinle eşleşmek zorundadır; bellekte kalmış eski
izin kayıt oluşturamaz. Public CPU API yolu da aynı yeniden yayımlama sınırına
tabidir; operatörün mevcut CPU ilk-kayıt yolu korunur.

Registry'den kaldırma yeni/yeniden devralınan işin kabulünü etkiler. Zaten
çalışan araştırmayı durdurmak için kimlikli stop akışı kullanılır; policy silme,
alt süreçlerin durduğu veya GPU'nun bırakıldığı kanıtı değildir.

## Kurulu veride kayıt önizlemesi

2026-10-03 CPU dağıtımı sonrasında, mevcut SKAB DEV snapshot
`964641ae824487aca2a83890bbec794a1eb8a19afed114b13b20b803660e0101` üzerinde
operatör komutunun yalnız önizleme yolu gerçek dosyalarla exit 0 verdi.
Taslak: smoke, 1 öneri, 1200 saniye, 8000 model token üst sınırı;
sağlayıcı yapılandırması `e483f42104afebb5791dfdf91caa4328f941a4300d433dbe3c82a82ba2dffb80`.
Bunlar önerilen sınırlar olup onaylanmış/çalıştırılmış araştırma değildir.
`registered=false`; registry öncesi/sonrası hash'i aynı. İzin, kaynak rezervasyonu,
model çalıştırma veya GPU kabulü oluşmadı. Ham taslak özel runtime dizinindedir.
