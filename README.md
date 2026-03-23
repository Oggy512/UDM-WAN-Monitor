# UDM-WAN-Monitor
DOCSight module for monitoring the WAN state of an Ubiquiti UDM Pro

UDM Monitor Dashboard integrated as own Link the the DOCSight menu panel:

<img width="296" height="152" alt="image" src="https://github.com/user-attachments/assets/d8ad1ba4-e1df-4e09-99b5-0a009c8b4361" />

The Dashboard has 3 information tabs:


1) Information regarding the both WAN interfaces

<img width="1446" height="698" alt="image" src="https://github.com/user-attachments/assets/6e185c8a-eb14-4bed-871d-711b2cf968fa" />


2) General information regarding the device (the UDM) itself

<img width="1435" height="562" alt="image" src="https://github.com/user-attachments/assets/924b83ff-e9c6-43c5-ad56-71665de7088f" />


3) Eventlog of WAN related events (failover, online/offline, ...)

<img width="1387" height="353" alt="image" src="https://github.com/user-attachments/assets/fd2f6c98-2347-4b0d-aeba-baa9c7565d0c" />

Those events are also written in the global DOCsight eventlog.

# Configuration

On the config site, one has to enter the IP (or hostname) of the UDM management interface and port (if not 443).
The module needs a User on the UDP. That can either be the admin account or a separate docsight account. Read only permissions are sufficient.
If the default TLS certificate is used, disable SSL verification.

<img width="955" height="842" alt="image" src="https://github.com/user-attachments/assets/48a75716-55fc-4331-89a9-63c0705c5c8e" />

Optional, one can montor tow additional ports:

<img width="905" height="458" alt="image" src="https://github.com/user-attachments/assets/55daeee5-5a11-4171-9ffe-d5d7dc3473c9" />
