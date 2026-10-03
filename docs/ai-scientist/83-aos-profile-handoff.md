# AOS oturumuna aktarılacak kısa özet

Scientist note82 adayında Bonsai response, AOS'un seçtiği note80 biçimiyle
aynı canonical şemayı kullanır: isteğe bağlı model/assistant rolü varsa korunur.
Dış response şema SHA:
`c07e0f14089840858b6a1d688fac81ba0dc9ae07870ef2c6a501ab110080467d`.

Infer altı alanlı integer wire1 olarak kalır. İç Bonsai içerik
`response_schema_sha256` pini dış şema hash'iyle değiştirilmez.

Yeni kontrollü infer için Scientist profil/config ve özgün admission pininde
ayrıca şu kapalı alan gerekir:

```json
{"output_contract":{"name":"aos-scientist-profile-output.v2","version":2,"bundle_sha256":"ab94aaf3fa70a82cde3971b16c325bc87bf3813d7e20c7dd4cea5d3e12e3962e"}}
```

Bundle sekiz tam şemayı ve projector/request-binding modül kaynak hash'ini
pinler; yeni sürümün kabulü eski terminal sonuçlarını normalize etmez.
AOS'un dört alanlı yeni admission-history profil pini bu beşinci alanı
henüz içermiyor. Ortak tam bundle/source/pin teyidi ve uygun yeni deployment
kimliği gerekiyor. AOS dosyalarına Scientist oturumu dokunmadı.

Scientist sonucu kaydetmeden önce özgün istek, seçenek/kanıt/capture kimlikleri,
token sınırları ve allocation fence'e bağlı bağımsız child generation'ıyla
kontrol eder; kayıtlı sonucu tekrar okurken aynı kontrolü uygular. İsteğe özgü
response/result şema hash'leri aynı arbiter DB'de değiştirilemez kayıtlıdır.
İlk birleşik CPU kontrolü 173 test geçti; native/GPU kabulü değildir.

Cleanup grant ayrı ve hedefe bağlıdır; infer yetkisi oluşturmaz. Caller restart
geçmiş yetki devri, integer saat
sürümü ve tam resolution journal açık. Idle/stopACK release kanıtı değildir.

Son kaynak/kalite kanıtı note82 ve kalite kapısı dosyasında takip edilir. Ortak
runtime kabulü verilmedi. Entegre GPU koşusunu yalnız Scientist oturumu,
kullanıcı işiyle çakışma olmadığında ve mevcut scheduler rezervasyonu altında
çalıştıracaktır; AOS oturumu ayrı bir GPU kabul koşusu başlatmamalıdır.

1 Ekim kaynak eki: [note85](85-aos-cleanup-capacity.md) admission sırasında
kontrol/cleanup kapasitesi rezervini ve hedefe bağlı kalıcı capability yanıtını
ekler. Global doluluk hedefin ayrılan payını tüketemez; bu sınırlı kaynak/CPU
güvencesidir. Yeni kontrol descriptor SHA
`110838e17f1ea899067d9c9ec61769b6fe26a607ecac986f83786b9f8943068d`.
Özgün bütçe/kota retry ile yenilenmez; target capability infer yetkisi vermez.
180 odaklı kontrol ve 1640 testlik zorunlu kapı geçti. Bu kesin descriptor
hash'i de ortak teyit bekler; native/GPU veya caller restart kabulü değildir.
