# Otomatik stop ve public yayın hazırlığı

Tarih: 2026-09-30. Ana kurulum **0.41.0** olarak korunur.

## Otomatik stop adayı

İzole kaynak adayında direkt dispatcher ve normal queue drain için mevcut
normal recovery yoluna bağlı otomatik kapanış eklendi. Nesil, fiziksel süreç
kimliği, ilk stop isteğinin 120 saniyelik sınırı ve özgün koşu son tarihi
korunur. Adayın yedi komutluk kapısı gerçek exit 0: **1265 passed,
7 skipped, 120 deselected**. Bu ana kurulumun dağıtılmış sürümü değildir.

Yeni, boş PostgreSQL 0035 veritabanı ve dört bağımsız sentetik aile kuruldu;
her aile 256 eğitim / 8192 değerlendirme örneği içerir. Model veya GPU
çalıştırılmadı; AOS fixture karar motoru ve desktop kullanıldı.

İlk bakım sürücüsü denemesi, JSON tuple/list karşılaştırması nedeniyle
**exit 1** verdi; ana servis durdurulmadı. Düzeltilmiş sürücünün gerçek
ikinci denemesi, canlı Scorer job/process neslini iki SQL okuması ve systemd
kimliğiyle gördü. Typed AOS stop ve aynı yetkiyle stop tekrarı geçti.
Otomatik yeni işçi / terminal rapor kontrolü başarısız oldu: **observer
exit 1, bakım exit 1**. Bu kabul kapatılmaz.

Somut neden: API durumu önceden `stop_requested` yaptığında hata kapanışı
RPC'si yalnız `running` kabul ettiği için dispatcher sonucu dönmeden hata
veriyor; drain kendi exit 75 / yeniden başlama yoluna ulaşamıyor. Düzeltme
ayrı kaynak türevinde sürüyor; SQL sahiplik kapısı gevşetilmiyor.

Bakımın **ana servisi geri getirme doğrulaması geçti**. Ana API ve konsol
nesilleri, özgün yapılandırma/kaynak sınırları ve iki eski sahipliksiz kayıt
korundu. Başarısız yeni koşu ve bütün özel kanıtlar saklandı. Direkt yol,
kalibre edilmiş aday/holdout stop ve gerçek model/GPU birlikte çalışma
kabulleri açık kalır.

## Public GitHub teslimi

Kullanıcı `aserdargun` hesabında public yayın istedi. İlk cihaz girişinin
süresi doldu; sonraki kontrolde yerel CLI'nin `aserdargun` oturumu geçerli
olarak doğrulandı. [Public depo](https://github.com/aserdargun/ai-scientist)
oluşturuldu ve temiz ilk kaynak commit'i
`6a6b681cbd6ff383687200a4617c973b22c2d375` normal push ile `main` dalına
gönderildi. Uzak Git ref, GitHub commit/tree, boş parent listesi, `PUBLIC`
görünürlük ve varsayılan `main` ayrı okumalarla doğrulandı. İlk public
snapshot 1602 dosya içerir; yayın kapısı **1247 test / yedi komut / exit 0**.

Yayın taraması 104 erişilebilir yerel commit ve
2409 ayrı Git blobunu kapsadı; güncel özel kimlik bilgilerinden eşleşme yok.
Eski bir SQL hata receipt'inde bulunan tarihsel işçi anahtarı public
dosyada temizlendi; özgün kayıt özel alanda korundu ve receipt'e redaksiyon
notu eklendi. Tarihsel başarısız sonuç değiştirilmedi.

Eski Git geçmişi yerelde korunur; public teslim temizlenmiş kaynak
snapshot'ıyla başladı. Runtime, ham özel kayıtlar, kimlik bilgileri,
veri setleri ve model ağırlıkları teslimin parçası değildir. Proje lisansı
henüz seçilmedi; public depo oluşturulması lisans seçimi yerine geçmez.
Sonraki public değişiklikler bu temiz geçmişten devam etmelidir. Eski yerel
geliştirme dalı veya tüm ref'ler public remote'a gönderilmez; kullanıcılar
public depoyu clone/fork ederek bağımsız çalışabilir.
