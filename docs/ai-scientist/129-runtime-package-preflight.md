# Çalışma imajı ve CPU güncelleme hazırlığı

Kaynak: `8f9a10bc6e6b7e665df48f74348b30e9d3ad6dff`.
İmaj: `sha256:e5118a99e2a6c5821e45d9f63ae6add094744b802a709fbd1975887cdf1a938b` (yerel, registry'ye yüklenmedi).

212 paket dosyası kaynakla byte düzeyinde eşleşti. İmaj içindeki `image.lock`,
derleme girdisinin önceki pinidir; host'ta seçilecek yeni imaj yukarıdaki digest'tir.
Kullanıcının yalnız Scientist CPU profili onayıyla host image lock güncellendi ve
yeni backend devreye alındı. Paket sürümü v0.1.0; iç harness sürümü 0.47.0.

Ağsız, GPU aygıtı olmayan, salt okunur root ile UID 10001 altında 1 CPU / 768 MiB
sınırında LSH, OPTICS ve SOM çalıştı: her biri 64 sentetik fit satırından sonra
12 satır için sonlu OMR üretti. Bu yalnız imaj duman kontrolüdür; gerçek veri,
Director/Scorer bağımsız puanlaması, yerel dil modeli veya AOS kabulü değildir.
İnceleme konteyneri başlatılmadan kaldırıldı; smoke konteyneri exit 0 ile kapandı
ve kaldırıldı. İki ilk stdin context denemesi Dockerfile çözümlemesinde başarısız
oldu; minimum dizin context'i ile derleme geçti. Bağımlılık katmanı cache'ten geldi.

## Kontrollü uygulama planı

- Hedef yalnız Scientist `field-lab` CPU profili; kuyruk ve kaynak kimlikleri yeniden doğrulanır.
- Mevcut profile stop yordamı kabulü kapatır ve kuyrukta yarışan yeni iş varsa devam etmez.
- Değişecek 16 kaynak/pin dosyası, eski frontend index ve private geri dönüş kopyası hazırlanır.
- İncelenen kaynak, yeni image digest ve derlenmiş frontend uygulanır; kayıtlı veriler/izinler korunur.
- CPU profil açılır; API kimliği, geçmiş raporlar, arayüz ve model-kapalı durumu doğrulanır.
- Hata halinde yalnız aynı profil ve tam yedek üzerinden geri dönülür. AOS süreci/kaynağı değiştirilmez.

`aos_native_launch.py` hedefte farklı bir eski sürümdedir; CPU güncellemesine
alınmaz. Native başlatıcı kendi ayrı sözleşme/kaynak incelemesini bekler.
Diğer runtime kaynaklarında beklenmeyen fark bulunmadı.

Kullanıcı “Evet, yalnız Scientist CPU profilini güncelle” yanıtıyla onay verdi.
Sahipliği doğrulanmış profil stop ve start komutları exit 0 ile tamamlandı.
16 kaynak/pin dosyası ve frontend uygulandı; private geri dönüş yedeği korundu.
API, console ve director-drain aktif; PostgreSQL açık; kuyruk 0/0/0.
Model çağrıları kapalı, public yerel eylemci izni henüz kurulu değil. AOS değiştirilmedi.

Canlı harness SHA-256: `20f4e5f22a40a11ecece78ad40fb6788106897fcab68ade133480332ab7e1df9`.
Önceki public CPU raporu hash doğrulamasıyla okunabildi. Canlı masaüstü (1440×1050)
ve mobil (390×844) tarayıcı kontrollerinde JavaScript hatası veya yatay taşma yok.
Yedi adımlı topoloji, yerel eylemci/manual CPU ayrımı ve isteğe bağlı,
onaysız/çalıştırılmamış öğretmen taslağı doğrulandı; hiçbir POST/model çağrısı yapılmadı.
Mevcut tünel korunuyor; uzak Mac tarayıcısından erişim ayrıca doğrulanmadı.

AOS publication checkout'unda gözlenen yeni commit `627d410dc7f16ffbeae401c6420141137f28d3a6`.
Typed CPU console startup ve optional-context yüzeyi kaynakta var; kendi
belgesi gerçek Scientist çağrısı/native birlikte çalışma kabulünü açık bırakıyor.
Bu gözlem AOS'un yeni Scientist descriptor'ını onayladığı anlamına gelmez.

[Makine okunur paket kaydı](review-evidence/runtime-package-8f9a10b-20261003.json).

## İstemci bağlantı scriptleri

Mac/Linux `connect-lab.sh --profile field-lab` ve Windows
`connect-lab.ps1 -LabProfile field-lab`, uzak başlatıcıya aynı profili gönderir
ve CPU konsolunun 8789 portuna tünel açar. Yerel port varsayılan 8788'dir.
Profilsiz eski davranış korunur. Geçersiz profil shell komutuna eklenmeden
reddedilir; `--no-start-lab` yalnız tünel kurar. Güncel komut README'dedir.

Bash sözdizimi ve gerçek SSH kullanmayan altı argv/hata kontrolü geçti.
CachyOS SSH listener'ı açık gözlendi; uzak Mac kimlik doğrulaması/tarayıcı
kabulü yapılmadı. PowerShell çalıştırma ortamı olmadığı için Windows kabulü
yapılmadı. Scriptler hazır kurulumlara kopyalandı; servis veya tünel yeniden
başlatılmadı, harness/imaj pini değişmedi.

- Bash SHA-256: `d856261f3c7378cbfc698115d544c71e9a7bd3befda0697678bfb67efc41c405`.
- PowerShell SHA-256: `1dfe01c75161d71dedcc3c21530bc12a5102590c8fcfacaa82b662de75d29902`.

## English / Türkçe arayüz teslimi

Kullanıcının dil isteğiyle frontend varsayılanı İngilizce yapıldı. Üst başlıktaki
**Language / Dil** seçimi Türkçe'yi korur; tercih `ai-scientist.locale.v1`
anahtarıyla tarayıcıda saklanır. Depolama engelliyse oturum içinde seçim çalışır.
Dil değişimi form değerlerini veya API istek kimliklerini değiştirmez. Metin,
tarih, sayı ve erişilebilirlik etiketleri çevrildi; özgün rapor/veri ve kullanıcının
yazdığı alanlar kaynak dilinde kalır.

TypeScript + Vite build exit 0. Mevcut Playwright ile Chromium'da önce aday,
sonra canlı arayüz kontrol edildi: altı sayfa; Türkçe tarayıcıda İngilizce varsayılan;
EN/TR geçişi; yeniden yükleme ve sekmeler arası tercih; depolama engeli; hedef
alanlarının korunması. 1440×1050 ve 390×844 görünümünde hata/taşma yok; hiçbir
POST/deney/model isteği yapılmadı. Browser eklentisi bu oturumda mevcut değildi.

Canlı field-lab'a yalnız frontend kaynak/asset/index uygulandı; index en son
atomik değiştirildi ve önceki hashli assetler açık sekmeler için korundu. Backend,
imaj, model yetkisi ve servis süreçleri değişmedi; yeniden başlatma olmadı.
Eski ana çalışma ağacının farklı UI kaynakları üzerine yazılmadı. Aserdargun
üzerindeki gerçek tarayıcı/Safari/Firefox kabulü ayrıca yapılmadı.

[Kaynak ve tarayıcı kontrol kaydı](review-evidence/frontend-language-20261003.json).

Dil tesliminin son kalite kapısı da exit 0: **3644 passed / 49 skipped / 177 deselected**;
yedi komut başarılı ve dondurulan kaynak değişmedi. Bu, önceki kapıya eklenen
yeni test toplamı değildir; aynı mevcut test havuzunun son kaynakla tekrarıdır.
