# AOS çıktı doğrulamasının yürütücüye bağlanması

30 Eylül 2026 kaynak çalışması; ortak runtime admission veya GPU kabulü değildir.
Scientist başlangıç HEAD'i `65b8fae6e58875d9640728994c6263e3d706ad01`;
AOS salt okunur HEAD'i `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
Her iki tarafta yerel değişiklik vardır; bu HEAD çifti yeni kaynakları kapsamaz.

## Uygulanan bağımsız parçalar

- Yeni infer kabulü özgün admission kaydında kapalı `output_contract` pini ister.
  Tarihsel dört alanlı profil pinleri mevcut kayıtların kanıtıdır; yeni infer
  yetkisi oluşturmaz.
- Politika değiştiğinde temizleme, özgün hedef ve özgün kabul/tombstone hash'ine
  bağlı ayrı, sınırlı grant kullanabilir. Bu grant infer yetkisi değildir.
  Aynı caller generation şartı korunur; yeniden başlatılmış caller için geçmiş
  yetki devralma uygulanmadı.
- Çıktı modülü özgün istek, deployment, seçenek kimlikleri, içerik, token sınırı
  ve doğrulanmış alt süreç generation'ına bağlı doğrulama sağlar. Modül tek
  başına scheduler veya GPU tahsis yetkisi oluşturmaz.

İlk birleşik bağımsız parça kontrolü: **145 CPU testi geçti, 6.92 saniye**;
ana süreç exit0, 7.099766417 saniye, Python kaynak hash'leri değişmedi.
Bu kontrol sonraki yürütücü entegrasyonundan önce yapıldı; yeni entegrasyonun
geçtiğini veya gerçek modelin çalıştığını kanıtlamaz.

## Karşı tarafla biçim kararı

AOS 20:31 UTC handoff'u note80 biçimini seçmiştir: Bonsai çıktısında model ve
assistant rolü isteğe bağlıdır; varsa korunur ve doğrulanır. Scientist bu güncel
seçime uyarlanır. Önceki taslakta zorunlu rol ve modelin kaldırılması vardı;
bu farklılık ortak admission verilmeden giderilir. Kaydedilmiş terminal
sonuçları başka biçime yeniden projekte edilmez.

AOS'un bildirdiği dış şema hash'i:
`c07e0f14089840858b6a1d688fac81ba0dc9ae07870ef2c6a501ab110080467d`.
Bonsai iç içerik `response_schema_sha256` pini korunur. Ayrı çıktı bundle'ı
konfigürasyon ve özgün kabul kaydına bağlanır; Scientist aday bundle hash'i
`ab94aaf3fa70a82cde3971b16c325bc87bf3813d7e20c7dd4cea5d3e12e3962e`;
modül kaynak hash'i
`1021e5b87dfb3154dc92ee109324f7e010ff3b55f15623859bfc455efb714b71`.
Bunlar henüz ortak runtime kabulü verilmiş pinler değildir.

## Yürütücü bağlantısı

Profil yükleyici kapalı çıktı pinini alır; registry paket, modül ve iç içerik
pinlerini doğrular. Kontrollü infer pini olmadan kaydolamaz veya GPU tahsis
alamaz. `aos_gpu_output_bindings` aynı arbiter DB içinde özgün istek bytes/hash,
principal, config, bundle ve isteğe özgü response/result şema hash'lerini saklar.
Kayıt yetkili intent'ten sonra, scheduler etkilerinden önce yapılır; retry bu
alanları değiştiremez veya eski kaydı benimseyemez.

Model cevabı persistence öncesinde projekte edilir; sonuç yazımı ve replay ayrıca
özgün istek, allocation fence ve bağımsız child binding generation'ını doğrular.
Replay kaydedilmiş sonucu normalize etmez. Wheel kalite kapısı sekiz şema,
bundle ve doğrulama modülünün paket içinde bulunmasını zorunlu kılar.

Birleşik yürütücü kontrolü: **173 CPU testi geçti, 8.36 saniye**; ana süreç
exit0, 8.539589998 saniye, Python kaynak hash'leri değişmedi. Bu sentetik
SQLite/worker gözlemleriyle kaynak entegrasyonu kanıtıdır; native/GPU değildir.

## Açık maddeler

- Yürütücü/profil yükleyici bağlantısı ve zorunlu kalite kapısı kaynak
  doğrulamasını geçti. Native/GPU kabulü hâlâ açık.
- AOS'un profil pin/journal kaydına çıktı bundle'ını bağlaması ve ortak kesin
  sürüm/hash teyidi gerekli; AOS dosyaları bu oturumda değiştirilmedi.
- Cleanup grant limitleri global kontrol kimliği kapasitesi rezervasyonu
  değildir. 100000 kimlik tavanında cleanup kapasitesini güvenceye alma açık.
- Yeni caller generation'a yetki devri, integer saat sürümü, tam resolution
  journal ve ilgili ortak sözleşme maddeleri açık.
- Kontrollü gerçek AOS → Scientist → bağımsız skor → doğrulanmış rapor/GPU
  bırakımı koşusu çalıştırılmadı. Kaynak kabulü, uygun boş host ve scheduler
  rezervasyonu olmadan model başlatılmaz. Idle veya stop ACK release kanıtı
  değildir; cleanup kanıtlanamazsa mevcut quarantine korunur.

Lisans ve genel CI işleri bu GPU kabulünden ayrı takip edilir.

## Son kalite kapısı

Yedi komutun tümü exit0; ana süreç exit0, 85.648588494 saniye ve Python
kaynak hash'leri değişmedi. 1618 test geçti, yedi açık opt-in test atlandı,
120 GPU/live test seçilmedi. Strict mypy 149 kaynak dosyasında geçti; wheel
sekiz şema/bundle/modül içerik kontrolü ve import smoke kontrolünü geçti.
İlk kapıda Ruff/mypy hataları vardı; biçimlendirme ve nullable yetki alanlarının
kapalı hata kontrolleri düzeltilerek kapı tekrar çalıştırıldı.

Bu teslim sırasında yeni model, GPU koşusu veya eğitim başlatılmadı. Model/
quantization ayarı, VRAM tepesi ve GPU bekleme/devir gecikmeleri ölçülmedi;
sentetik worker/cleanup gözlemleri gerçek GPU bırakımı kanıtı olarak sunulmaz.
Gerçek koşu için bağlanan profil bağlam üst sınırı 16384, çıktı üst sınırı512;
özel profile daha küçük sınırlar uygulanabilir. Lisans/genel CI ayrı kalır.
